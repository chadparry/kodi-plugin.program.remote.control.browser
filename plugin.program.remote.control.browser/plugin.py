#!/usr/bin/python
# -*- coding: utf-8 -*-
import argparse
import bs4
import collections
import contextlib
import datetime
import json
import os
import pipes
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import threading
import urllib
import urllib2
import urlparse
import uuid
import xbmc
import xbmcplugin
import xbmcgui
import xbmcaddon
import xml.etree.ElementTree


# If any of these packages are missing, the script will attempt to proceed
# without those features.
try:
    import alsaaudio
except ImportError:
    alsaaudio = None
try:
    import psutil
except ImportError:
    psutil = None
try:
    import pylirc
except ImportError:
    pylirc = None


VOLUME_MIN = 0L
VOLUME_MAX = 100L
DEFAULT_VOLUME_STEP = 1L
RELEASE_KEY_DELAY = datetime.timedelta(seconds=1)
BROWSER_EXIT_DELAY = datetime.timedelta(seconds=3)


PylircCode = collections.namedtuple('PylircCode', ('config', 'repeat'))


FetchedWebpage = collections.namedtuple('FetchedWebpage', ('soup', 'error', 'url'))


class JsonRpcError(RuntimeError):
    pass


class WebpageExtractionError(RuntimeError):
    pass


class KodiMixer:
    """Mixer that integrates tightly with Kodi volume controls"""

    def __init__(self):
        if alsaaudio is None:
            xbmc.log('Not initializing an alsaaudio mixer')
        else:
            self.delegate = alsaaudio.Mixer()
        self.lastRpcId = 0
        try:
            result = self.executeJSONRPC('Application.GetProperties',
                {'properties': ['muted', 'volume']})
            self.muted = bool(result['muted'])
            self.volume = int(result['volume'])
        except (JsonRpcError, KeyError, ValueError) as e:
            xbmc.log('Could not retrieve current volume: ' + str(e))
            self.muted = False
            self.volume = VOLUME_MAX

    def __enter__(self):
        if alsaaudio is not None:
            self.original = self.delegate.getvolume()
        # Match the current volume to Kodi's last volume.
        self.realizeVolume()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # Restore the master volume to its original level.
        if alsaaudio is not None:
            for (channel, volume) in enumerate(self.original):
                self.delegate.setvolume(volume, channel)

    def getNextRpcId(self):
        self.lastRpcId = self.lastRpcId + 1
        return self.lastRpcId

    def executeJSONRPC(self, method, params):
        response = xbmc.executeJSONRPC(json.dumps({
            'jsonrpc': '2.0',
            'method': method,
            'params': params,
            'id': self.getNextRpcId()}))
        try:
            return json.loads(response)['result']
        except (KeyError, ValueError):
            raise JsonRpcError('Invalid JSON RPC response: ' + repr(response))

    def realizeVolume(self):
        # Muting the Master volume and then unmuting it is not a symmetric
        # operation, because other controls end up muted. So a mute needs to be
        # simulated by setting the volume level to zero.
        if alsaaudio is not None:
            if self.muted:
                self.delegate.setvolume(0)
            else:
                self.delegate.setvolume(self.volume)

    def toggleMute(self):
        try:
            result = self.executeJSONRPC('Application.SetMute', {'mute': 'toggle'})
            self.muted = bool(result)
        except (JsonRpcError, ValueError) as e:
            xbmc.log('Could not toggle mute: ' + str(e))
            self.muted = not self.muted
        self.realizeVolume()

    def incrementVolume(self):
        self.muted = False
        try:
            result = self.executeJSONRPC('Application.SetVolume', {'volume': 'increment'})
            self.volume = int(result)
        except (JsonRpcError, ValueError) as e:
            xbmc.log('Could not increase volume: ' + str(e))
            self.volume = min(self.volume + DEFAULT_VOLUME_STEP, VOLUME_MAX)
        if alsaaudio is not None:
            self.delegate.setvolume(self.volume)

    def decrementVolume(self):
        try:
            result = self.executeJSONRPC('Application.SetVolume', {'volume': 'decrement'})
            self.volume = int(result)
        except (JsonRpcError, ValueError) as e:
            xbmc.log('Could not decrease volume: ' + str(e))
            self.volume = max(self.volume - DEFAULT_VOLUME_STEP, VOLUME_MIN)
        if alsaaudio is not None and not self.muted:
            self.delegate.setvolume(self.volume)


@contextlib.contextmanager
def suspendXbmcLirc():
    xbmc.log('Suspending XBMC LIRC', xbmc.LOGDEBUG)
    xbmc.executebuiltin("LIRC.Stop")
    try:
        yield
    finally:
        xbmc.log('Resuming XBMC LIRC', xbmc.LOGDEBUG)
        xbmc.executebuiltin("LIRC.Start")


@contextlib.contextmanager
def runPylirc(name, configuration):
    if pylirc is None:
        xbmc.log('Not initializing pylirc')
        yield
        return
    fd = pylirc.init(name, configuration)
    if not fd:
        raise RuntimeError('Failed to initialize pylirc')
    try:
        yield fd
    finally:
        pylirc.exit()


def monitorProcess(proc, exitSocket):
    proc.wait()
    exitSocket.shutdown(socket.SHUT_RDWR)


def getProcessTree(parent):
    if psutil is None:
        xbmc.log('Not searching for process descendents', xbmc.LOGDEBUG)
        return [parent]
    try:
        process = psutil.Process(parent)
        processes = [process] + process.get_children(recursive=True)
        return [node.pid for node in processes]
    except psutil.NoSuchProcess:
        xbmc.log('Failed to find process tree', xbmc.LOGDEBUG)
        return []


def killBrowser(proc, sig):
    for pid in getProcessTree(proc.pid):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


@contextlib.contextmanager
def runBrowser(browserCmd):
    (sink, source) = socket.socketpair()
    with contextlib.closing(sink), contextlib.closing(source):
        waiter = None
        try:
            if xbmc.getCondVisibility('system.platform.windows'):
                # On Windows, the Popen will block unless close_fds is True and
                # creationflags is DETACHED_PROCESS.
                xbmc.log('Using Windows creation flags', xbmc.LOGDEBUG)
                creationflags = 0x00000008
            else:
                creationflags = 0
            xbmc.log('Launching browser: ' + ' '.join(pipes.quote(arg) for arg in browserCmd), xbmc.LOGINFO)
            proc = subprocess.Popen(browserCmd, creationflags=creationflags, close_fds=True)
            try:
                # Monitor the browser and kick the socket when it exits.
                waiterStarting = threading.Thread(target=monitorProcess, args=(proc, source))
                waiterStarting.start()
                waiter = waiterStarting

                yield (proc, sink.fileno())

                # Ask each child process to exit.
                xbmc.log('Terminating the browser', xbmc.LOGDEBUG)
                killBrowser(proc, signal.SIGTERM)

                # Give the browser a few seconds to shut down gracefully.
                def terminateBrowser():
                    xbmc.log('Forcefully killing the browser at the deadline', xbmc.LOGINFO)
                    killBrowser(proc, signal.SIGKILL)
                terminator = threading.Timer(BROWSER_EXIT_DELAY.total_seconds(), terminateBrowser)
                terminator.start()
                try:
                    proc.wait()
                    proc = None
                    xbmc.log('Waited for the browser to quit', xbmc.LOGDEBUG)
                finally:
                    terminator.cancel()
                    terminator.join()
            finally:
                if proc is not None:
                    # As a last resort, forcibly kill the browser.
                    xbmc.log('Forcefully killing the browser', xbmc.LOGINFO)
                    killBrowser(proc, signal.SIGKILL)
                    proc.wait()
                    xbmc.log('Waited for the browser to die', xbmc.LOGDEBUG)
        finally:
            if waiter is not None:
                xbmc.log('Joining with browser monitoring thread', xbmc.LOGDEBUG)
                waiter.join()
                xbmc.log('Joined with browser monitoring thread', xbmc.LOGDEBUG)


def activateWindow(cmd, proc, aborting, xdotoolPath):
    (output, error) = proc.communicate()
    if aborting.is_set():
        xbmc.log('Aborting search for browser PID', xbmc.LOGDEBUG)
        return
    if proc.returncode != 0:
        xbmc.log('Failed search for browser PID', xbmc.LOGINFO)
        raise subprocess.CalledProcessError(proc.returncode, cmd, output)
    wids = [wid for wid in output.split('\n') if wid]
    for wid in wids:
        if aborting.is_set():
            xbmc.log('Aborting activation of windows', xbmc.LOGDEBUG)
            return
        xbmc.log('Activating window with WID: ' + wid, xbmc.LOGDEBUG)
        subprocess.call([xdotoolPath, 'WindowActivate', wid])
    xbmc.log('Finished activating windows', xbmc.LOGDEBUG)


@contextlib.contextmanager
def raiseBrowser(pid, xdotoolPath):
    if not xdotoolPath:
        xbmc.log('Not raising the browser')
        yield
        return
    activator = None
    # With the "sync" flag, the command could block indefinitely.
    cmd = [xdotoolPath, 'search', '--sync', '--onlyvisible', '--pid', str(pid)]
    xbmc.log('Searching for browser PID: ' + ' '.join(pipes.quote(arg) for arg in cmd), xbmc.LOGINFO)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, universal_newlines=True)
    try:
        aborting = threading.Event()
        activatorStarting = threading.Thread(target=activateWindow, args=(cmd, proc, aborting, xdotoolPath))
        activatorStarting.start()
        activator = activatorStarting

        yield
    finally:
        aborting.set()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if activator is not None:
            activator.join()


def runRemoteControlBrowser(lircConfig, browserCmd, xdotoolPath):
    with (
            suspendXbmcLirc()), (
            runPylirc("browser", lircConfig)) as lircFd, (
            KodiMixer()) as mixer, (
            runBrowser(browserCmd)) as (browser, browserExitFd), (
            raiseBrowser(browser.pid, xdotoolPath)):

        releaseKeyTime = None
        repeatKeys = None
        isExiting = False
        while not isExiting:
            if releaseKeyTime is None:
                timeout = None
            else:
                timeout = max((releaseKeyTime - datetime.datetime.now()).total_seconds(), 0)
            polling = [browserExitFd]
            if lircFd is not None:
                polling.append(lircFd)
            (rlist, wlist, xlist) = select.select(polling, [], [], timeout)
            if browserExitFd in rlist:
                # The browser exited prematurely.
                break
            if lircFd is not None and lircFd in rlist:
                buttons = pylirc.nextcode(True)
            else:
                buttons = None
            codes = [PylircCode(**button) for button in buttons] if buttons else []
            if releaseKeyTime is not None and releaseKeyTime <= datetime.datetime.now():
                codes.append(PylircCode(config='RELEASE', repeat=0))

            for code in codes:
                xbmc.log('Received LIRC code: ' + str(code), xbmc.LOGDEBUG)
                tokens = code.config.split()
                (command, args) = (tokens[0], tokens[1:])
                isReleasing = False
                nextReleaseKeyTime = None
                inputs = None

                if command == 'VOLUME_UP':
                    mixer.incrementVolume()
                elif command == 'VOLUME_DOWN':
                    mixer.decrementVolume()
                elif command == 'MUTE':
                    mixer.toggleMute()
                elif command == 'MULTITAP':
                    if releaseKeyTime is not None and repeatKeys == args:
                        repeatIndex += 1
                    else:
                        isReleasing = True
                        repeatKeys = args
                        repeatIndex = 0
                    nextReleaseKeyTime = datetime.datetime.now() + RELEASE_KEY_DELAY
                    current = args[repeatIndex % len(args)]
                    inputs = ['key', '--clearmodifiers', '--', current, 'Shift+Left']
                elif command == 'KEY':
                    isReleasing = True
                    inputs = ['key', '--clearmodifiers', '--'] + args
                elif command == 'CLICK':
                    isReleasing = True
                    inputs = ['click', '--clearmodifiers', '1']
                elif command == 'MOUSE':
                    step = min(code.repeat, 10)
                    (horizontal, vertical) = args
                    acceleratedHorizontal = str(int(horizontal) * step ** 2)
                    acceleratedVertical = str(int(vertical) * step ** 2)
                    inputs = ['mousemove_relative', '--', acceleratedHorizontal, acceleratedVertical]
                elif command == 'EXIT':
                    inputs = ['key', 'Alt+F4']
                    isExiting = True
                elif command == 'RELEASE':
                    isReleasing = True
                else:
                    raise RuntimeError('Unrecognized LIRC config: ' + command)

                if isReleasing and releaseKeyTime is not None:
                    if xdotoolPath:
                        # Deselect the current multi-tap character.
                        xbmc.log('Executing xdotool for multi-tap release', xbmc.LOGDEBUG)
                        subprocess.check_call([xdotoolPath, 'key', '--clearmodifiers', 'Right'])
                    else:
                        xbmc.log('Ignoring xdotool for multi-tap release', xbmc.LOGDEBUG)
                releaseKeyTime = nextReleaseKeyTime

                if inputs is not None:
                    if xdotoolPath:
                        cmd = [xdotoolPath] + inputs
                        xbmc.log('Executing: ' + ' '.join(cmd), xbmc.LOGDEBUG)
                        subprocess.check_call(cmd)
                    else:
                        xbmc.log('Ignoring xdotool inputs: ' + str(inputs), xbmc.LOGDEBUG)


class RemoteControlBrowserPlugin(xbmcaddon.Addon):

    def __new__(cls, *args, **kwargs):
        # The Addon.__new__ override doesn't accept additional arguments.
        return super(RemoteControlBrowserPlugin, cls).__new__(cls)

    def __init__(self, pluginUrl, handle):
        super(RemoteControlBrowserPlugin, self).__init__()
        self.pluginUrl = pluginUrl
        self.handle = handle
        self.addonFolder = xbmc.translatePath(self.getAddonInfo('path')).decode('utf_8')
        self.profileFolder = xbmc.translatePath(self.getAddonInfo('profile')).decode('utf_8')
        self.bookmarksPath = os.path.join(self.profileFolder, 'bookmarks.xml')
        self.defaultBookmarksPath = os.path.join(self.addonFolder, 'resources/data/bookmarks.xml')
        self.thumbsFolder = os.path.join(self.profileFolder, 'thumbs')
        self.defaultThumbsFolder = os.path.join(self.addonFolder, 'resources/data/thumbs')

    def buildPluginUrl(self, query):
        components = urlparse.urlparse(self.pluginUrl)
        return urlparse.ParseResult(
            scheme=components.scheme,
            netloc=components.netloc,
            path=components.path,
            params=components.params,
            query=urllib.urlencode(query),
            fragment=None).geturl()

    def makedirs(self, folder):
        try:
            os.makedirs(folder)
        except OSError:
            # The directory may already exist.
            xbmc.log('Failed to create directory: ' + folder, xbmc.LOGDEBUG)
            pass

    def getThumbPath(self, thumbId, thumbsFolder=None):
        if thumbsFolder is None:
            thumbsFolder = self.thumbsFolder
        return os.path.join(thumbsFolder, thumbId + '.png')

    def readBookmarks(self):
        try:
            return xml.etree.ElementTree.parse(self.bookmarksPath)
        except (IOError, xml.etree.ElementTree.ParseError) as e:
            xbmc.log('Falling back to default bookmarks: ' + str(e), xbmc.LOGDEBUG)
            return xml.etree.ElementTree.parse(self.defaultBookmarksPath)

    def getBookmarkElement(self, tree, bookmarkId):
        bookmark = tree.find('bookmark[@id="{}"]'.format(bookmarkId))
        if bookmark is None:
            raise ValueError('Unrecognized bookmark ID: ' + bookmarkId)
        return bookmark

    def getBookmarkDirectoryItem(self, bookmarkId, title, thumbId):
        url = self.buildPluginUrl({'mode': 'launchBookmark', 'id': bookmarkId})
        # A zero-width space is used to escape label metacharacters. Other means of
        # escaping, such as "$LBRACKET", don't work in this context.
        escapedTitle = re.sub('[][$]', u'\\g<0>\u200B', title)
        listItem = xbmcgui.ListItem(label=escapedTitle)
        if thumbId is not None:
            thumbPath = self.getThumbPath(thumbId)
            if not os.path.isfile(thumbPath):
                thumbPath = self.getThumbPath(thumbId, self.defaultThumbsFolder)
            if os.path.isfile(thumbPath):
                listItem.setArt({
                    'thumb': thumbPath,
                })
        listItem.addContextMenuItems([
            (self.getLocalizedString(30025), 'RunPlugin({})'.format(self.buildPluginUrl(
                {'mode': 'launchBookmark', 'id': bookmarkId}))),
            (self.getLocalizedString(30006), 'RunPlugin({})'.format(self.buildPluginUrl(
                {'mode': 'editBookmark', 'id': bookmarkId}))),
            (self.getLocalizedString(30002), 'RunPlugin({})'.format(self.buildPluginUrl(
                {'mode': 'removeBookmark', 'id': bookmarkId}))),
        ])
        # Kodi lets addons render their own folders.
        isFolder = True
        return (url, listItem, isFolder)

    def index(self):
        tree = self.readBookmarks()
        items = [self.getBookmarkDirectoryItem(
            bookmark.get('id'),
            bookmark.get('title'),
            bookmark.get('thumb'))
            for bookmark in tree.iter('bookmark')]

        url = self.buildPluginUrl({'mode': 'addBookmark'})
        listItem = xbmcgui.ListItem('[B]{}[/B]'.format(self.getLocalizedString(30001)))
        listItem.setArt({
            'thumb': 'DefaultFile.png',
        })
        isFolder = True
        items.append((url, listItem, isFolder))

        success = xbmcplugin.addDirectoryItems(handle=self.handle, items=items, totalItems=len(items))
        if not success:
            raise RuntimeError('Failed addDirectoryItem')
        xbmcplugin.endOfDirectory(self.handle)

    def removeThumb(self, thumbId):
        if thumbId is not None:
            try:
                os.remove(self.getThumbPath(thumbId))
            except OSError:
                xbmc.log('Failed to remove thumbnail: ' + thumbId, xbmc.LOGINFO)
                pass

    def fetchWebpage(self, url):
        try:
            xbmc.log('Fetching webpage: ' + url, xbmc.LOGINFO)
            fd = urllib2.urlopen(url)
            return FetchedWebpage(bs4.BeautifulSoup(fd, 'html.parser'), None, fd.geturl())
        except (ValueError, IOError) as e:
            return FetchedWebpage(None, WebpageExtractionError(str(e)), url)

    def inputBookmark(self, savedBookmarkId=None, defaultUrl='http://', defaultTitle=None):
        webpage = None
        keyboard = xbmc.Keyboard(defaultUrl, self.getLocalizedString(30004))
        keyboard.doModal()
        if not keyboard.isConfirmed():
            xbmc.log('User aborted URL input', xbmc.LOGDEBUG)
            return
        url = keyboard.getText()

        if defaultTitle is None:
            if webpage is None:
                webpage = self.fetchWebpage(url)
            try:
                if webpage.error is not None:
                    raise webpage.error
                titleElement = webpage.soup.find('title')
                if titleElement is None:
                    raise WebpageExtractionError('Webpage has no title element')
                defaultTitle = titleElement.text
            except WebpageExtractionError as e:
                xbmc.log('Failed to extract title from bookmarked page: ' + str(e))
                defaultTitle = urlparse.urlparse(url).netloc

        keyboard = xbmc.Keyboard(defaultTitle, self.getLocalizedString(30003))
        keyboard.doModal()
        if not keyboard.isConfirmed():
            xbmc.log('User aborted title input', xbmc.LOGDEBUG)
            return
        title = keyboard.getText()

        if savedBookmarkId is None:
            bookmarkId = str(uuid.uuid1())
        else:
            bookmarkId = savedBookmarkId

        # Save the bookmark metadata.
        tree = self.readBookmarks()
        if savedBookmarkId is None:
            xml.etree.ElementTree.SubElement(tree.getroot(), 'bookmark', {
                'id': bookmarkId,
                'title': title,
                'url': url,
            })
        else:
            bookmark = self.getBookmarkElement(tree, bookmarkId)
            bookmark.set('title', title)
            bookmark.set('url', url)
        self.makedirs(self.profileFolder)
        tree.write(self.bookmarksPath)
        xbmc.executebuiltin("Container.Refresh")

        # Try to download an icon for the bookmark.
        try:
            if webpage is None:
                webpage = self.fetchWebpage(url)
            if webpage.error is None:
                # Search for a rel="icon" attribute.
                linkElements = webpage.soup.findAll('link', rel='icon', href=True)
                # Prefer the icon with the best quality.
                linkElement = next(
                    iter(sorted(
                        linkElements,
                        key=lambda element: (
                            # Prefer large images.
                            reduce(lambda prev, cur: prev * int(cur, 10), re.findall(r'\d+', element['sizes']), 1)
                                if 'sizes' in element.attrs else 0,
                            # Prefer PNG format.
                            'type' in element.attrs and element['type'] == 'image/png',
                            # Prefer "icon" to "shortcut icon".
                            element['rel'] == ['icon']),
                        reverse=True)),
                    None)
            else:
                xbmc.log('Failed to open webpage: ' + str(webpage.error))
                linkElement = None
            if linkElement is None:
                xbmc.log('Falling back to default favicon path', xbmc.LOGDEBUG)
                link = '/favicon.ico'
            else:
                link = linkElement['href']
            thumbUrl = urlparse.urljoin(webpage.url, link)

            self.makedirs(self.thumbsFolder)

            # The Pillow module needs to be isolated to its own subprocess because
            # many distributions are prone to deadlock.
            retrievePath = os.path.join(self.addonFolder, 'retrieve.py')
            # The old thumb ID can't be reused, because then the cached copy of the
            # old image would never be replaced.
            thumbId = str(uuid.uuid1())
            thumbPath = self.getThumbPath(thumbId)
            xbmc.log('Retrieving favicon: ' + thumbUrl, xbmc.LOGINFO)
            subprocess.check_call([sys.executable, retrievePath, thumbUrl, thumbPath])
        except (WebpageExtractionError, ValueError, subprocess.CalledProcessError) as e:
            # Any previously downloaded thumbnail will be retained.
            xbmc.log('Failed to retrieve favicon: ' + str(e))
            thumbId = None

        if thumbId is not None:
            tree = self.readBookmarks()
            bookmark = self.getBookmarkElement(tree, bookmarkId)
            removeThumbId = bookmark.get('thumb')
            bookmark.set('thumb', thumbId)
            tree.write(self.bookmarksPath)
            xbmc.executebuiltin("Container.Refresh")
            self.removeThumb(removeThumbId)

    def addBookmark(self):
        self.inputBookmark()

    def editBookmark(self, bookmarkId):
        tree = self.readBookmarks()
        bookmark = self.getBookmarkElement(tree, bookmarkId)
        self.inputBookmark(bookmarkId, bookmark.get('url'), bookmark.get('title'))

    def removeBookmark(self, bookmarkId):
        tree = self.readBookmarks()
        bookmark = self.getBookmarkElement(tree, bookmarkId)
        thumbId = bookmark.get('thumb')
        tree.getroot().remove(bookmark)
        self.makedirs(self.profileFolder)
        tree.write(self.bookmarksPath)
        self.removeThumb(thumbId)

        xbmc.executebuiltin("Container.Refresh")

    def launchBookmark(self, bookmarkId):
        tree = self.readBookmarks()
        bookmark = self.getBookmarkElement(tree, bookmarkId)
        url = bookmark.get('url')
        xbmc.Player().stop()

        browserPath = self.getSetting("browserPath")
        browserArgs = self.getSetting("browserArgs")
        xdotoolPath = self.getSetting("xdotoolPath")

        if not browserPath or not os.path.isfile(browserPath):
            xbmc.executebuiltin('XBMC.Notification(Info:,{}!,5000)'.format(self.getLocalizedString(30005)))
            self.openSettings()
            return

        # Flashing a white screen looks bad, but it is improved with a black
        # interstitial page.
        blackPath = os.path.join(self.addonFolder, 'resources/data/black.html')
        blackUrl = urlparse.ParseResult(
            scheme='file',
            netloc=None,
            path=blackPath,
            params=None,
            query=urllib.quote_plus(url),
            fragment=None).geturl()

        browserCmd = [browserPath] + shlex.split(browserArgs) + [blackUrl]

        lircConfig = os.path.join(self.addonFolder, "resources/data/browser.lirc")
        runRemoteControlBrowser(lircConfig, browserCmd, xdotoolPath)


def parsedParams(search):
    query = search[1:]
    if query:
        return urlparse.parse_qs(query, strict_parsing=True)
    else:
        return {}


def getBookmarkId(args):
    bookmarkId = next(iter(args.params.get('id', [])), None)
    if bookmarkId is None:
        raise ValueError('Missing bookmark ID')
    # Validate the ID.
    uuid.UUID(bookmarkId)
    return bookmarkId


def main():
    xbmc.log('Plugin called: ' + ' '.join(pipes.quote(arg) for arg in sys.argv), xbmc.LOGDEBUG)
    # The Kodi Python customizations broke automatic detection of prog.
    parser = argparse.ArgumentParser(prog=sys.argv[0])
    parser.add_argument('handle', type=int)
    parser.add_argument('params', type=parsedParams)
    args = parser.parse_args()

    plugin = RemoteControlBrowserPlugin(parser.prog, args.handle)

    mode = next(iter(args.params.get('mode', ['index'])), None)
    xbmc.log('Parsed mode: ' + mode, xbmc.LOGDEBUG)
    if mode == 'index':
        plugin.index()
    elif mode == 'addBookmark':
        plugin.addBookmark()
    elif mode == 'launchBookmark':
        plugin.launchBookmark(getBookmarkId(args))
    elif mode == 'removeBookmark':
        plugin.removeBookmark(getBookmarkId(args))
    elif mode == 'editBookmark':
        plugin.editBookmark(getBookmarkId(args))
    else:
        raise ValueError('Unrecognized mode: ' + mode)


if __name__ == "__main__":
    main()
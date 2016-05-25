#!/usr/bin/python
# -*- coding: utf-8 -*-
import bs4
import collections
import contextlib
import datetime
import io
import json
import os
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
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

addon = xbmcaddon.Addon()
pluginPath = sys.argv[0]
pluginhandle = int(sys.argv[1])
addonID = addon.getAddonInfo('id')
addonPath = addon.getAddonInfo('path')
translation = addon.getLocalizedString
xdotoolPath = addon.getSetting("xdotoolPath")

userDataFolder = xbmc.translatePath("special://profile/addon_data/"+addonID)
bookmarksPath = os.path.join(userDataFolder, 'bookmarks.xml')
defaultBookmarksPath = os.path.join(addonPath, 'resources/data/bookmarks.xml')
thumbsFolder = os.path.join(userDataFolder, 'thumbs')

if not os.path.isdir(userDataFolder):
    os.mkdir(userDataFolder)

VOLUME_MIN = 0L
VOLUME_MAX = 100L
DEFAULT_VOLUME_STEP = 1L
RELEASE_KEY_DELAY = datetime.timedelta(seconds=1)
BROWSER_EXIT_DELAY = datetime.timedelta(seconds=3)

PylircCode = collections.namedtuple('PylircCode', ('config', 'repeat'))
FetchedWebpage = collections.namedtuple('FetchedWebpage', ('soup', 'error', 'url'))

def getProcessTree(parent):
    if psutil is None:
        return [parent]
    try:
        process = psutil.Process(parent)
        processes = [process] + process.get_children(recursive=True)
        return [node.pid for node in processes]
    except psutil.NoSuchProcess:
        return []

@contextlib.contextmanager
def suspendXbmcLirc():
    xbmc.executebuiltin("LIRC.Stop")
    try:
        yield
    finally:
        xbmc.executebuiltin("LIRC.Start")

@contextlib.contextmanager
def runPylirc(name, configuration):
    if pylirc is None:
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
                creationflags = 0x00000008
            else:
                creationflags = 0
            proc = subprocess.Popen(browserCmd, creationflags=creationflags, close_fds=True)
            try:
                # Monitor the browser and kick the socket when it exits.
                waiterStarting = threading.Thread(target=monitorProcess, args=(proc, source))
                waiterStarting.start()
                waiter = waiterStarting

                yield (proc, sink.fileno())

                # Ask each child process to exit.
                killBrowser(proc, signal.SIGTERM)

                # Give the browser a few seconds to shut down gracefully.
                terminator = threading.Timer(BROWSER_EXIT_DELAY.total_seconds(),
                    lambda: killBrowser(proc, signal.SIGKILL))
                terminator.start()
                try:
                    proc.wait()
                    proc = None
                finally:
                    terminator.cancel()
                    terminator.join()
            finally:
                if proc is not None:
                    # As a last resort, forcibly kill the browser.
                    killBrowser(proc, signal.SIGKILL)
                    proc.wait()
        finally:
            if waiter is not None:
                waiter.join()

def activateWindow(cmd, proc, aborting):
    (output, error) = proc.communicate()
    if aborting.is_set():
        return
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output)
    wids = [wid for wid in output.split('\n') if wid]
    for wid in wids:
        if aborting.is_set():
            return
        subprocess.call([xdotoolPath, 'WindowActivate', wid])

@contextlib.contextmanager
def raiseBrowser(pid):
    activator = None
    # With the "sync" flag, the command could block indefinitely.
    cmd = [xdotoolPath, 'search', '--sync', '--onlyvisible', '--pid', str(pid)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, universal_newlines=True)
    try:
        aborting = threading.Event()
        activatorStarting = threading.Thread(target=activateWindow, args=(cmd, proc, aborting))
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

class JsonRpcError(RuntimeError):
    pass

class KodiMixer:
    """Mixer that integrates tightly with Kodi volume controls"""
    def __init__(self):
        if alsaaudio is not None:
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


def readBookmarks():
    try:
        return xml.etree.ElementTree.parse(bookmarksPath)
    except (IOError, xml.etree.ElementTree.ParseError) as e:
        xbmc.log('Falling back to default bookmarks: ' + str(e), xbmc.LOGDEBUG)
        return xml.etree.ElementTree.parse(defaultBookmarksPath)


def getBookmarkElement(tree, bookmarkId):
    if '"' in bookmarkId:
        raise ValueError('Invalid bookmark: ' + bookmarkId)
    bookmark = tree.find('bookmark[@id="{}"]'.format(bookmarkId))
    if bookmark is None:
        raise ValueError('Unrecognized bookmark ID: ' + bookmarkId)
    return bookmark


def getBookmarkDirectoryItem(bookmarkId, title, thumbId):
    url = pluginPath + '?' + urllib.urlencode({'mode': 'launchBookmark', 'id': bookmarkId})
    listItem = xbmcgui.ListItem(label=title)
    if thumbId is not None:
        thumbPath = os.path.join(thumbsFolder, thumbId + '.png')
        if os.path.exists(thumbPath):
            listItem.setArt({
                'thumb': thumbPath,
            })
    listItem.addContextMenuItems([
        (translation(30025), 'RunPlugin({})'.format(pluginPath + '?' +
            urllib.urlencode({'mode': 'launchBookmark', 'id': bookmarkId}))),
        (translation(30006), 'RunPlugin({})'.format(pluginPath + '?' +
            urllib.urlencode({'mode': 'editBookmark', 'id': bookmarkId}))),
        (translation(30002), 'RunPlugin({})'.format(pluginPath + '?' +
            urllib.urlencode({'mode': 'removeBookmark', 'id': bookmarkId}))),
    ])
    # Kodi lets addons render their own folders.
    isFolder = True
    return (url, listItem, isFolder)


def index():
    tree = readBookmarks()
    items = [getBookmarkDirectoryItem(
        bookmark.get('id'),
        bookmark.get('title'),
        bookmark.get('thumb'))
        for bookmark in tree.iter('bookmark')]

    url = pluginPath + '?' + urllib.urlencode({'mode': 'addBookmark'})
    listItem = xbmcgui.ListItem('[B]{}[/B]'.format(translation(30001)))
    listItem.setArt({
        'thumb': 'DefaultFile.png',
    })
    isFolder = True
    items.append((url, listItem, isFolder))

    success = xbmcplugin.addDirectoryItems(handle=pluginhandle, items=items, totalItems=len(items))
    if not success:
        raise RuntimeError('Failed addDirectoryItem')
    xbmcplugin.endOfDirectory(pluginhandle)


def removeThumb(thumbId):
    if thumbId is not None:
        try:
            os.remove(os.path.join(thumbsFolder, thumbId + '.png'))
        except OSError:
            pass

def fetchWebpage(url):
    try:
        fd = urllib2.urlopen(url)
        return FetchedWebpage(bs4.BeautifulSoup(fd, 'html.parser'), None, fd.geturl())
    except (ValueError, IOError) as e:
        return FetchedWebpage(None, WebpageExtractionError(str(e)), url)


class WebpageExtractionError(RuntimeError):
    pass


def inputBookmark(savedBookmarkId=None, defaultUrl='http://', defaultTitle=None):
    webpage = None
    keyboard = xbmc.Keyboard(defaultUrl, translation(30004))
    keyboard.doModal()
    if not keyboard.isConfirmed():
        return
    url = keyboard.getText()

    if defaultTitle is None:
        if webpage is None:
            webpage = fetchWebpage(url)
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

    keyboard = xbmc.Keyboard(defaultTitle, translation(30003))
    keyboard.doModal()
    if not keyboard.isConfirmed():
        return
    title = keyboard.getText()

    if savedBookmarkId is None:
        bookmarkId = str(uuid.uuid1())
    else:
        bookmarkId = savedBookmarkId

    try:
        if re.search(r'[^\w-]', bookmarkId):
            raise ValueError('Invalid bookmark: ' + bookmarkId)
        if webpage is None:
            webpage = fetchWebpage(url)
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
            link = '/favicon.ico'
        else:
            link = linkElement['href']
        thumbUrl = urlparse.urljoin(webpage.url, link)

        try:
            os.makedirs(thumbsFolder)
        except OSError:
            # The directory may already exist.
            pass

        # The Pillow module needs to be isolated to its own subprocess because
        # many distributions are prone to deadlock.
        retrievePath = os.path.join(addonPath, 'retrieve.py')
        # The old thumb ID can't be reused, because then the cached copy of the
        # old image would never be replaced.
        thumbId = str(uuid.uuid1())
        thumbPath = os.path.join(thumbsFolder, thumbId + '.png')
        subprocess.check_call([sys.executable, retrievePath, thumbUrl, thumbPath])
    except (WebpageExtractionError, ValueError, subprocess.CalledProcessError) as e:
        # Any previously downloaded thumbnail will be retained.
        xbmc.log('Failed to retrieve favicon: ' + str(e))
        thumbId = None

    tree = readBookmarks()
    if savedBookmarkId is None:
        xml.etree.ElementTree.SubElement(tree.getroot(), 'bookmark', {
            'id': bookmarkId,
            'title': title,
            'url': url,
            'thumb': thumbId,
        })
    else:
        bookmark = getBookmarkElement(tree, bookmarkId)
        bookmark.set('title', title)
        bookmark.set('url', url)
        if thumbId is None:
            removeThumbId = None
        else:
            removeThumbId = bookmark.get('thumb')
            bookmark.set('thumb', thumbId)
    tree.write(bookmarksPath)
    removeThumb(removeThumbId)

    xbmc.executebuiltin("Container.Refresh")


def addBookmark():
    inputBookmark()


def editBookmark(bookmarkId):
    tree = readBookmarks()
    bookmark = getBookmarkElement(tree, bookmarkId)
    inputBookmark(bookmarkId, bookmark.get('url'), bookmark.get('title'))


def removeBookmark(bookmarkId):
    tree = readBookmarks()
    bookmark = getBookmarkElement(tree, bookmarkId)
    thumbId = bookmark.get('thumb')
    tree.getroot().remove(bookmark)
    tree.write(bookmarksPath)
    removeThumb(thumbId)

    xbmc.executebuiltin("Container.Refresh")


def launchBookmark(bookmarkId):
    tree = readBookmarks()
    bookmark = getBookmarkElement(tree, bookmarkId)
    url = bookmark.get('url')
    xbmc.Player().stop()

    browserPath = addon.getSetting("browserPath")
    browserArgs = addon.getSetting("browserArgs")

    if not browserPath or not os.path.exists(browserPath):
        xbmc.executebuiltin('XBMC.Notification(Info:,'+str(translation(30005))+'!,5000)')
        addon.openSettings()
        return

    # Flashing a white screen on switching to chrome looks bad, so I'll use a temp html file with black background
    # to redirect to our desired location.
    blackPath = os.path.join(userDataFolder, "black.html")
    blackUrl = 'file://' + urllib.quote(blackPath)
    with open(blackPath, "w") as blackFile:
        blackFile.write('<html><body style="background:black"><script>window.location.href = "%s";</script></body></html>' % url)

    browserCmd = [browserPath] + shlex.split(browserArgs) + [blackUrl]

    lircConfig = os.path.join(addonPath, "resources/data/browser.lirc")
    with (
            suspendXbmcLirc()), (
            runPylirc("browser", lircConfig)) as lircFd, (
            KodiMixer()) as mixer, (
            runBrowser(browserCmd)) as (browser, browserExitFd), (
            raiseBrowser(browser.pid)):

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
                    # Deselect the current multi-tap character.
                    xbmc.log('Executing xdotool for multi-tap release', xbmc.LOGDEBUG)
                    subprocess.check_call([xdotoolPath, 'key', '--clearmodifiers', 'Right'])
                releaseKeyTime = nextReleaseKeyTime

                if inputs is not None:
                    cmd = [xdotoolPath] + inputs
                    xbmc.log('Executing: ' + ' '.join(cmd), xbmc.LOGDEBUG)
                    subprocess.check_call(cmd)


def parameters_string_to_dict(parameters):
    paramDict = {}
    if parameters:
        paramPairs = parameters[1:].split("&")
        for paramsPair in paramPairs:
            paramSplits = paramsPair.split('=')
            if (len(paramSplits)) == 2:
                paramDict[paramSplits[0]] = paramSplits[1]
    return paramDict


params = parameters_string_to_dict(sys.argv[2])
mode = urllib.unquote_plus(params.get('mode', ''))
name = urllib.unquote_plus(params.get('name', ''))
bookmarkId = urllib.unquote_plus(params.get('id', ''))


if mode == 'addBookmark':
    addBookmark()
elif mode == 'launchBookmark':
    launchBookmark(bookmarkId)
elif mode == 'removeBookmark':
    removeBookmark(bookmarkId)
elif mode == 'editBookmark':
    editBookmark(bookmarkId)
else:
    index()

#!/usr/bin/python
# -*- coding: utf-8 -*-
import collections
import contextlib
import datetime
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
import xbmc
import xbmcplugin
import xbmcgui
import xbmcaddon

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
pluginhandle = int(sys.argv[1])
addonID = addon.getAddonInfo('id')
addonPath = addon.getAddonInfo('path')
translation = addon.getLocalizedString
xdotoolPath = addon.getSetting("xdotoolPath")

userDataFolder = xbmc.translatePath("special://profile/addon_data/"+addonID)
siteFolder = os.path.join(userDataFolder, 'sites')

if not os.path.isdir(userDataFolder):
    os.mkdir(userDataFolder)
if not os.path.isdir(siteFolder):
    os.mkdir(siteFolder)

VOLUME_MIN = 0L
VOLUME_MAX = 100L
DEFAULT_VOLUME_STEP = 1L
RELEASE_KEY_DELAY = datetime.timedelta(seconds=1)
BROWSER_EXIT_DELAY = datetime.timedelta(seconds=3)
youtubeUrl = "http://www.youtube.com/leanback"
vimeoUrl = "http://www.vimeo.com/couchmode"

PylircCode = collections.namedtuple('PylircCode', ('config', 'repeat'))

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


def index():
    files = os.listdir(siteFolder)
    for file in files:
        if file.endswith(".link"):
            fh = open(os.path.join(siteFolder, file), 'r')
            title = ""
            url = ""
            thumb = ""
            for line in fh.readlines():
                entry = line[:line.find("=")]
                content = line[line.find("=")+1:]
                if entry == "title":
                    title = content.strip()
                elif entry == "url":
                    url = content.strip()
                elif entry == "thumb":
                    thumb = content.strip()
            fh.close()
            addSiteDir(title, url, 'showSite', thumb)
    addDir("[ Vimeo Couchmode ]", vimeoUrl, 'showSite', os.path.join(addonPath, "vimeo.png"))
    addDir("[ Youtube Leanback ]", youtubeUrl, 'showSite', os.path.join(addonPath, "youtube.png"))
    addDir("[B]- "+translation(30001)+"[/B]", "", 'addSite', "")
    xbmcplugin.endOfDirectory(pluginhandle)


def addSite(site="", title=""):
    if site:
        filename = getFileName(title)
        content = "title="+title+"\nurl="+site+"\nthumb=DefaultFolder.png"
        fh = open(os.path.join(siteFolder, filename+".link"), 'w')
        fh.write(content)
        fh.close()
    else:
        keyboard = xbmc.Keyboard('', translation(30003))
        keyboard.doModal()
        if keyboard.isConfirmed() and keyboard.getText():
            title = keyboard.getText()
            keyboard = xbmc.Keyboard('http://', translation(30004))
            keyboard.doModal()
            if keyboard.isConfirmed() and keyboard.getText():
                url = keyboard.getText()
                content = "title="+title+"\nurl="+url+"\nthumb=DefaultFolder.png"
                fh = open(os.path.join(siteFolder, getFileName(title)+".link"), 'w')
                fh.write(content)
                fh.close()
    xbmc.executebuiltin("Container.Refresh")


def getFileName(title):
    return (''.join(c for c in unicode(title, 'utf-8') if c not in '/\\:?"*|<>')).strip()


def launchBrowser(browserCmd):
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

def showSite(url):
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
    launchBrowser(browserCmd)


def removeSite(title):
    os.remove(os.path.join(siteFolder, getFileName(title)+".link"))
    xbmc.executebuiltin("Container.Refresh")


def editSite(title):
    filenameOld = getFileName(title)
    file = os.path.join(siteFolder, filenameOld+".link")
    fh = open(file, 'r')
    title = ""
    url = ""
    thumb = "DefaultFolder.png"
    for line in fh.readlines():
        entry = line[:line.find("=")]
        content = line[line.find("=")+1:]
        if entry == "title":
            title = content.strip()
        elif entry == "url":
            url = content.strip()
        elif entry == "thumb":
            thumb = content.strip()
    fh.close()

    oldTitle = title
    keyboard = xbmc.Keyboard(title, translation(30003))
    keyboard.doModal()
    if keyboard.isConfirmed() and keyboard.getText():
        title = keyboard.getText()
        keyboard = xbmc.Keyboard(url, translation(30004))
        keyboard.doModal()
        if keyboard.isConfirmed() and keyboard.getText():
            url = keyboard.getText()
            content = "title="+title+"\nurl="+url+"\nthumb="+thumb
            fh = open(os.path.join(siteFolder, getFileName(title)+".link"), 'w')
            fh.write(content)
            fh.close()
            if title != oldTitle:
                os.remove(os.path.join(siteFolder, filenameOld+".link"))
    xbmc.executebuiltin("Container.Refresh")


def parameters_string_to_dict(parameters):
    paramDict = {}
    if parameters:
        paramPairs = parameters[1:].split("&")
        for paramsPair in paramPairs:
            paramSplits = paramsPair.split('=')
            if (len(paramSplits)) == 2:
                paramDict[paramSplits[0]] = paramSplits[1]
    return paramDict


def addDir(name, url, mode, iconimage):
    u = sys.argv[0]+"?url="+urllib.quote_plus(url)+"&mode="+urllib.quote_plus(mode)
    ok = True
    liz = xbmcgui.ListItem(name, iconImage="DefaultFolder.png", thumbnailImage=iconimage)
    liz.setInfo(type="Video", infoLabels={"Title": name})
    ok = xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=u, listitem=liz, isFolder=True)
    return ok


def addSiteDir(name, url, mode, iconimage):
    u = sys.argv[0]+"?url="+urllib.quote_plus(url)+"&mode="+urllib.quote_plus(mode)
    ok = True
    liz = xbmcgui.ListItem(name, iconImage="DefaultFolder.png", thumbnailImage=iconimage)
    liz.setInfo(type="Video", infoLabels={"Title": name})
    liz.addContextMenuItems([(translation(30006), 'RunPlugin(plugin://'+addonID+'/?mode=editSite&url='+urllib.quote_plus(name)+')',), (translation(30002), 'RunPlugin(plugin://'+addonID+'/?mode=removeSite&url='+urllib.quote_plus(name)+')',)])
    ok = xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=u, listitem=liz, isFolder=True)
    return ok

params = parameters_string_to_dict(sys.argv[2])
mode = urllib.unquote_plus(params.get('mode', ''))
name = urllib.unquote_plus(params.get('name', ''))
url = urllib.unquote_plus(params.get('url', ''))


if mode == 'addSite':
    addSite()
elif mode == 'showSite':
    showSite(url)
elif mode == 'removeSite':
    removeSite(url)
elif mode == 'editSite':
    editSite(url)
else:
    index()

#!/usr/bin/python
# -*- coding: utf-8 -*-
import alsaaudio
import collections
import contextlib
import datetime
import json
import os
import psutil
import pylirc
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib
import xbmcplugin
import xbmcgui
import xbmcaddon


addon = xbmcaddon.Addon()
pluginhandle = int(sys.argv[1])
addonID = addon.getAddonInfo('id')
addonPath = addon.getAddonInfo('path')
translation = addon.getLocalizedString
osWin = xbmc.getCondVisibility('system.platform.windows')
osOsx = xbmc.getCondVisibility('system.platform.osx')
osLinux = xbmc.getCondVisibility('system.platform.linux')
useOwnProfile = addon.getSetting("useOwnProfile") == "true"
useCustomPath = addon.getSetting("useCustomPath") == "true"
customPath = xbmc.translatePath(addon.getSetting("customPath"))

userDataFolder = xbmc.translatePath("special://profile/addon_data/"+addonID)
profileFolder = os.path.join(userDataFolder, 'profile')
siteFolder = os.path.join(userDataFolder, 'sites')

if not os.path.isdir(userDataFolder):
    os.mkdir(userDataFolder)
if not os.path.isdir(profileFolder):
    os.mkdir(profileFolder)
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
def runBrowser(args, creationflags):
    (sink, source) = socket.socketpair()
    with contextlib.closing(sink), contextlib.closing(source):
        waiter = None
        try:
            proc = subprocess.Popen(args, creationflags=creationflags, close_fds=True)
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
        subprocess.call(['xdotool', 'WindowActivate', wid])

@contextlib.contextmanager
def raiseBrowser(pid):
    activator = None
    # With the "sync" flag, the command could block indefinitely.
    cmd = ['xdotool', 'search', '--sync', '--onlyvisible', '--pid', str(pid)]
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
        self.original = self.delegate.getvolume()
        # Match the current volume to Kodi's last volume.
        self.realizeVolume()
        return self
    def __exit__(self, exc_type, exc_value, traceback):
        # Restore the master volume to its original level.
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
        self.delegate.setvolume(self.volume)
    def decrementVolume(self):
        try:
            result = self.executeJSONRPC('Application.SetVolume', {'volume': 'decrement'})
            self.volume = int(result)
        except (JsonRpcError, ValueError) as e:
            xbmc.log('Could not decrease volume: ' + str(e))
            self.volume = max(self.volume - DEFAULT_VOLUME_STEP, VOLUME_MIN)
        if not self.muted:
            self.delegate.setvolume(self.volume)


def index():
    files = os.listdir(siteFolder)
    for file in files:
        if file.endswith(".link"):
            fh = open(os.path.join(siteFolder, file), 'r')
            title = ""
            url = ""
            thumb = ""
            kiosk = "yes"
            stopPlayback = "no"
            for line in fh.readlines():
                entry = line[:line.find("=")]
                content = line[line.find("=")+1:]
                if entry == "title":
                    title = content.strip()
                elif entry == "url":
                    url = content.strip()
                elif entry == "thumb":
                    thumb = content.strip()
                elif entry == "kiosk":
                    kiosk = content.strip()
                elif entry == "stopPlayback":
                    stopPlayback = content.strip()
            fh.close()
            addSiteDir(title, url, 'showSite', thumb, stopPlayback, kiosk)
    addDir("[ Vimeo Couchmode ]", vimeoUrl, 'showSite', os.path.join(addonPath, "vimeo.png"), "yes", "yes")
    addDir("[ Youtube Leanback ]", youtubeUrl, 'showSite', os.path.join(addonPath, "youtube.png"), "yes", "yes")
    addDir("[B]- "+translation(30001)+"[/B]", "", 'addSite', "")
    xbmcplugin.endOfDirectory(pluginhandle)


def addSite(site="", title=""):
    if site:
        filename = getFileName(title)
        content = "title="+title+"\nurl="+site+"\nthumb=DefaultFolder.png\nstopPlayback=no\nkiosk=yes"
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
                keyboard = xbmc.Keyboard('no', translation(30009))
                keyboard.doModal()
                if keyboard.isConfirmed() and keyboard.getText():
                    stopPlayback = keyboard.getText()
                    keyboard = xbmc.Keyboard('yes', translation(30016))
                    keyboard.doModal()
                    if keyboard.isConfirmed() and keyboard.getText():
                        kiosk = keyboard.getText()
                        content = "title="+title+"\nurl="+url+"\nthumb=DefaultFolder.png\nstopPlayback="+stopPlayback+"\nkiosk="+kiosk
                        fh = open(os.path.join(siteFolder, getFileName(title)+".link"), 'w')
                        fh.write(content)
                        fh.close()
    xbmc.executebuiltin("Container.Refresh")


def getFileName(title):
    return (''.join(c for c in unicode(title, 'utf-8') if c not in '/\\:?"*|<>')).strip()


def getFullPath(path, url, useKiosk, userAgent):
    profile = ""
    if useOwnProfile:
        profile = '--user-data-dir='+profileFolder
        if useKiosk=="yes" and osLinux:
            # On Linux, chrome kiosk leavs black bars on side/bottom of screen due to an incorrect working size.
            # We can fix the preferences directly
            # cat $prefs |perl -pe "s/\"work_area_bottom.*/\"work_area_bottom\": $(xrandr | grep \* | cut -d' ' -f4 | cut -d'x' -f2),/" > $prefs
            # cat $prefs |perl -pe "s/\"work_area_right.*/\"work_area_right\": $(xrandr | grep \* | cut -d' ' -f4 | cut -d'x' -f1),/" > $prefs
            try:
                width, height = 0,0
                xrandr = subprocess.check_output(['xrandr']).split('\n')
                for line in xrandr:
                    match = re.compile('([0-9]+)x([0-9]+).+?\*.+?').findall(line)
                    if match:
                        width = int(match[0][0])
                        height = int(match[0][1])
                        break
                prefs = os.path.join(profileFolder, 'Default', 'Preferences')
                # space for non existing controls. Not sure why it needs it, but it does on my setup
                top_margin = 30

                with open(prefs, "rb+") as prefsfile:
                    import json
                    prefsdata = json.load(prefsfile)
                    prefs_browser = prefsdata.get('browser', {})
                    prefs_window_placement = prefs_browser.get('window_placement', {})
                    prefs_window_placement['always_on_top'] = True
                    prefs_window_placement['top'] = top_margin
                    prefs_window_placement['bottom'] = height-top_margin
                    prefs_window_placement['work_area_bottom'] = height
                    prefs_window_placement['work_area_right'] = width
                    prefsdata['browser'] = prefs_browser
                    prefsdata['browser']['window_placement'] = prefs_window_placement
                    prefsfile.seek(0)
                    prefsfile.truncate(0)
                    json.dump(prefsdata, prefsfile, indent=4, separators=(',', ': '))

            except:
                xbmc.log("Can't update chrome resolution", xbmc.LOGINFO)

    # Flashing a white screen on switching to chrome looks bad, so I'll use a temp html file with black background
    # to redirect to our desired location.
    black_background = os.path.join(userDataFolder, "black.html")
    with open(black_background, "w") as launch:
        launch.write('<html><body style="background:black"><script>window.location.href = "%s";</script></body></html>' % url)

    kiosk = ""
    if useKiosk=="yes":
        kiosk = '--kiosk'
    if userAgent:
        userAgent = '--user-agent="'+userAgent+'"'
    
    #fullPath = '"'+path+'" '+profile+userAgent+'--start-maximized --disable-translate --disable-new-tab-first-run --no-default-browser-check --no-first-run '+kiosk+'"'+black_background+'"'
    fullPath = [path, profile, userAgent, '--start-maximized','--disable-translate','--disable-new-tab-first-run','--no-default-browser-check','--no-first-run', kiosk, black_background]
    for idx in range(0,len(fullPath))[::-1]:
        if not fullPath[idx]:
            del fullPath[idx]

    strpath = ""
    for arg in fullPath:
        strpath += " " + arg
    xbmc.log('Full Path: ' + str(strpath), xbmc.LOGDEBUG)
    return fullPath


def launchBrowser(fullUrl, creationflags):
    lircConfig = os.path.join(addonPath, "resources/data/browser.lirc")
    with (
            suspendXbmcLirc()), (
            runPylirc("browser", lircConfig)) as lircFd, (
            KodiMixer()) as mixer, (
            runBrowser(fullUrl, creationflags)) as (browser, browserExitFd), (
            raiseBrowser(browser.pid)):

        releaseKeyTime = None
        repeatKeys = None
        isExiting = False
        while not isExiting:
            if releaseKeyTime is None:
                timeout = None
            else:
                timeout = max((releaseKeyTime - datetime.datetime.now()).total_seconds(), 0)
            (rlist, wlist, xlist) = select.select([lircFd, browserExitFd], [], [], timeout)
            if browserExitFd in rlist:
                # The browser exited prematurely.
                break
            if lircFd in rlist:
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
                    subprocess.check_call(['xdotool', 'key', '--clearmodifiers', 'Right'])
                releaseKeyTime = nextReleaseKeyTime

                if inputs is not None:
                    cmd = ["xdotool"] + inputs
                    xbmc.log('Executing: ' + ' '.join(cmd), xbmc.LOGDEBUG)
                    subprocess.check_call(cmd)

def showSite(url, stopPlayback, kiosk, userAgent):
    chrome_path = ""
    creationflags = 0
    if stopPlayback == "yes":
        xbmc.Player().stop()
    if osWin:
        creationflags = 0x00000008 # DETACHED_PROCESS https://msdn.microsoft.com/en-us/library/windows/desktop/ms684863(v=vs.85).aspx
        path = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
        path64 = 'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe'
        if useCustomPath and os.path.exists(customPath):
            chrome_path = customPath
        elif os.path.exists(path):
            chrome_path = path
        elif os.path.exists(path64):
            chrome_path = path64
    elif osOsx:
        path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if useCustomPath and os.path.exists(customPath):
            chrome_path = customPath
        elif os.path.exists(path):
            chrome_path = path
    elif osLinux:
        path = "/usr/bin/google-chrome"
        if useCustomPath and os.path.exists(customPath):
            chrome_path = customPath
        elif os.path.exists(path):
            chrome_path = path

    if chrome_path:
        fullUrl = getFullPath(chrome_path, url, kiosk, userAgent)
        launchBrowser(fullUrl, creationflags)
    else:
        xbmc.executebuiltin('XBMC.Notification(Info:,'+str(translation(30005))+'!,5000)')
        addon.openSettings()


def removeSite(title):
    os.remove(os.path.join(siteFolder, getFileName(title)+".link"))
    xbmc.executebuiltin("Container.Refresh")


def editSite(title):
    filenameOld = getFileName(title)
    file = os.path.join(siteFolder, filenameOld+".link")
    fh = open(file, 'r')
    title = ""
    url = ""
    kiosk = "yes"
    thumb = "DefaultFolder.png"
    stopPlayback = "no"
    for line in fh.readlines():
        entry = line[:line.find("=")]
        content = line[line.find("=")+1:]
        if entry == "title":
            title = content.strip()
        elif entry == "url":
            url = content.strip()
        elif entry == "kiosk":
            kiosk = content.strip()
        elif entry == "thumb":
            thumb = content.strip()
        elif entry == "stopPlayback":
            stopPlayback = content.strip()
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
            keyboard = xbmc.Keyboard(stopPlayback, translation(30009))
            keyboard.doModal()
            if keyboard.isConfirmed() and keyboard.getText():
                stopPlayback = keyboard.getText()
                keyboard = xbmc.Keyboard(kiosk, translation(30016))
                keyboard.doModal()
                if keyboard.isConfirmed() and keyboard.getText():
                    kiosk = keyboard.getText()
                    content = "title="+title+"\nurl="+url+"\nthumb="+thumb+"\nstopPlayback="+stopPlayback+"\nkiosk="+kiosk
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


def addDir(name, url, mode, iconimage, stopPlayback="", kiosk=""):
    u = sys.argv[0]+"?url="+urllib.quote_plus(url)+"&mode="+urllib.quote_plus(mode)+"&stopPlayback="+urllib.quote_plus(stopPlayback)+"&kiosk="+urllib.quote_plus(kiosk)
    ok = True
    liz = xbmcgui.ListItem(name, iconImage="DefaultFolder.png", thumbnailImage=iconimage)
    liz.setInfo(type="Video", infoLabels={"Title": name})
    ok = xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=u, listitem=liz, isFolder=True)
    return ok


def addSiteDir(name, url, mode, iconimage, stopPlayback, kiosk):
    u = sys.argv[0]+"?url="+urllib.quote_plus(url)+"&mode="+urllib.quote_plus(mode)+"&stopPlayback="+urllib.quote_plus(stopPlayback)+"&kiosk="+urllib.quote_plus(kiosk)
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
stopPlayback = urllib.unquote_plus(params.get('stopPlayback', 'no'))
kiosk = urllib.unquote_plus(params.get('kiosk', 'yes'))
userAgent = urllib.unquote_plus(params.get('userAgent', ''))
profileFolderParam = urllib.unquote_plus(params.get('profileFolder', ''))
if profileFolderParam:
    useOwnProfile = True
    profileFolder = profileFolderParam


if mode == 'addSite':
    addSite()
elif mode == 'showSite':
    showSite(url, stopPlayback, kiosk, userAgent)
elif mode == 'removeSite':
    removeSite(url)
elif mode == 'editSite':
    editSite(url)
else:
    index()


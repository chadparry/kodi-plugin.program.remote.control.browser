#!/usr/bin/python
# -*- coding: utf-8 -*-
import alsaaudio
import contextlib
import datetime
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
debug = addon.getSetting("debug") == "true"

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
VOLUME_DEFAULT = 50L
VOLUME_STEP = 2L
RELEASE_KEY_DELAY = datetime.timedelta(seconds=1)
BROWSER_EXIT_DELAY = datetime.timedelta(seconds=3)
PYLIRC_CONFIG = "config"
PYLIRC_REPEAT = "repeat"
youtubeUrl = "http://www.youtube.com/leanback"
vimeoUrl = "http://www.vimeo.com/couchmode"

trace_on = False
try:
    with open(os.path.join(addonPath,'user_debug.py'),'a'): # touch file
        pass
    import user_debug
    trace_on = user_debug.enable_pydev()
except (ImportError, AttributeError) as ex:
   xbmc.log("Debug Disable")
   pass

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
                xbmc.log("Can't update chrome resolution")

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

    if debug:
        print "Full Path:"
        strpath = ""
        for arg in fullPath:
            strpath += " " + arg
        print strpath
    return fullPath


def launchBrowser(fullUrl, creationflags):
    lircConfig = os.path.join(addonPath, "resources/data/browser.lirc")
    with (
            suspendXbmcLirc()), (
            runPylirc("browser", lircConfig)) as lircFd, (
            runBrowser(fullUrl, creationflags)) as (browser, browserExitFd), (
            raiseBrowser(browser.pid)):

        mixer = alsaaudio.Mixer()
        lastvolume = mixer.getvolume()[0]
        mute = lastvolume == 0
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
                codes = pylirc.nextcode(True)
            else:
                codes = []
            if releaseKeyTime is not None and releaseKeyTime <= datetime.datetime.now():
                codes.append({PYLIRC_CONFIG: 'RELEASE', PYLIRC_REPEAT: 0})
                releaseKeyTime = None

            for code in codes:
                #print >>log, code
                if code is None:
                    continue
                config = code[PYLIRC_CONFIG].split()
                repeat = code[PYLIRC_REPEAT]
                if config[0] == "EXIT":
                    config = ['key', 'alt+F4']
                    isExiting = True
                if config[0] == "VOLUME_UP":
                    if mute:
                        mute = False
                        volume = lastvolume
                    else:
                        volume = mixer.getvolume()[0]
                    volume = min(volume + VOLUME_STEP, VOLUME_MAX)
                    mixer.setvolume(volume)
                    break
                if config[0] == "VOLUME_DOWN":
                    volume = mixer.getvolume()[0]
                    volume = max(volume - VOLUME_STEP, VOLUME_MIN)
                    mixer.setvolume(volume)
                    break
                if config[0] == "MUTE":
                    mute = not mute
                    if mute:
                        volume = VOLUME_MIN
                        lastvolume = mixer.getvolume()[0]
                    elif lastvolume == VOLUME_MIN:
                        volume = VOLUME_DEFAULT
                    else:
                        volume = lastvolume
                    mixer.setvolume(volume)
                    break
                if config[0] == "SMSJUMP":
                    keys = config[1:]
                    config = ['key', '--clearmodifiers', '--']
                    if releaseKeyTime is not None and repeatKeys == keys:
                        repeatIndex += 1
                    else:
                        if releaseKeyTime is not None:
                            config.append('Right')
                        repeatKeys = keys
                        repeatIndex = 0
                    current = keys[repeatIndex % len(keys)]
                    config.append(current)
                    config.append('Shift+Left')
                    subprocess.check_call(["xdotool"] + config)

                    releaseKeyTime = datetime.datetime.now() + RELEASE_KEY_DELAY
                    break
                if config[0] == "KEY":
                    keys = config[1:]
                    config = ['key', '--clearmodifiers', '--']
                    if releaseKeyTime is not None:
                        config.append('Right')
                        releaseKeyTime = None
                    config.extend(keys)
                if config[0] == "RELEASE":
                    config = ['key', 'Right']
                if config[0] == "mousemove_relative":
                    mousestep = min(repeat, 10)
                    config[2] = str(int(config[2]) * mousestep ** 2)
                    config[3] = str(int(config[3]) * mousestep ** 2)
                #print >>log, ["xdotool"] + config
                subprocess.check_call(["xdotool"] + config)
                #log.flush()

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

if trace_on:
    pydevd.stoptrace()

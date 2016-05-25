#!/usr/bin/python
# -*- coding: utf-8 -*-
import collections
import os
import subprocess
import sys
import xbmc
import xbmcaddon
import xml.etree.ElementTree

# These libraries must be installed manually instead of through a Kodi module
# because they are platform-dependent. The user will be shown a warning if they
# are not present.
try:
    import alsaaudio
except ImportError:
    xbmc.log('Missing Python package: pyalsaaudio')
    alsaaudio = None
try:
    import psutil
except ImportError:
    xbmc.log('Missing Python package: psutil')
    psutil = None
try:
    import pylirc
except ImportError:
    xbmc.log('Missing Python package: pylirc2')
    pylirc = None

addon = xbmcaddon.Addon()

DetectedDefaults = collections.namedtuple('DetectedDefaults', ('browserPath', 'browserArgs', 'xdotoolPath'))

def getDefaults():
    addonPath = addon.getAddonInfo('path')
    defaultsPath = os.path.join(addonPath, 'resources/data/dependencies.xml')
    tree = xml.etree.ElementTree.parse(defaultsPath)

    for platform in tree.iter('platform'):
        platformId = platform.get('id')
        if xbmc.getCondVisibility(platformId):
            browserPath = ''
            browserArgs = ''
            xdotoolPath = ''
            for xdotool in platform.iter('xdotool'):
                if os.path.exists(xdotool.get('path')):
                    xdotoolPath = xdotool.get('path')
                    break
            for browser in platform.iter('browser'):
                if os.path.exists(browser.get('path')):
                    browserPath = browser.get('path')
                    browserArgs = browser.get('args')
                    break
            return DetectedDefaults(browserPath, browserArgs, xdotoolPath)
    raise RuntimeError('Platform not supported')

def isWarningVisible(module):
    BOOL_ENCODING = { False: 'false', True: 'true' }
    return BOOL_ENCODING[not module]

def generateSettings():
    xbmc.log('Generating default addon settings')
    root = xml.etree.ElementTree.Element('settings')
    dependencies = xml.etree.ElementTree.SubElement(root, 'category', {'label': '30018'})
    defaults = getDefaults()

    # The Pillow module needs to be isolated to its own subprocess because many
    # distributions are prone to deadlock.
    addonPath = addon.getAddonInfo('path')
    importPath = os.path.join(addonPath, 'import.py')
    try:
        subprocess.check_call([sys.executable, importPath])
        pillow = True
    except subprocess.CalledProcessError:
        pillow = False

    xml.etree.ElementTree.SubElement(dependencies, 'setting', {
        'id': 'browserPath',
        'type': 'executable',
        'label': '30019',
        'default': defaults.browserPath })
    xml.etree.ElementTree.SubElement(dependencies, 'setting', {
        'id': 'browserArgs',
        'type': 'text',
        'label': '30020',
        'default': defaults.browserArgs })
    xml.etree.ElementTree.SubElement(dependencies, 'setting', {
        'id': 'xdotoolPath',
        'type': 'executable',
        'label': '30021',
        'default': defaults.xdotoolPath })
    xml.etree.ElementTree.SubElement(dependencies, 'setting', {
        'type': 'lsep',
        'label': '30022',
        'visible': isWarningVisible(psutil) })
    xml.etree.ElementTree.SubElement(dependencies, 'setting', {
        'type': 'lsep',
        'label': '30023',
        'visible': isWarningVisible(alsaaudio) })
    xml.etree.ElementTree.SubElement(dependencies, 'setting', {
        'type': 'lsep',
        'label': '30024',
        'visible': isWarningVisible(pylirc) })
    xml.etree.ElementTree.SubElement(dependencies, 'setting', {
        'type': 'lsep',
        'label': '30026',
        'visible': isWarningVisible(pillow) })

    tree = xml.etree.ElementTree.ElementTree(root)
    addonPath = addon.getAddonInfo('path')
    settingsPath = os.path.join(addonPath, 'resources/settings.xml')
    tree.write(settingsPath, encoding='utf-8', xml_declaration=True)

def main():
    generateSettings()

if __name__ == "__main__":
    main()

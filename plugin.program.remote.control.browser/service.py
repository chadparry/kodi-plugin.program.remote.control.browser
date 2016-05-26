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


DetectedDefaults = collections.namedtuple('DetectedDefaults', ('browserPath', 'browserArgs', 'xdotoolPath'))


class RemoteControlBrowserService(xbmcaddon.Addon):

    def __init__(self):
        super(RemoteControlBrowserService, self).__init__()
        self.addonFolder = xbmc.translatePath(self.getAddonInfo('path')).decode('utf_8')

    def getDefaults(self):
        dependenciesPath = os.path.join(self.addonFolder, 'resources/data/dependencies.xml')
        tree = xml.etree.ElementTree.parse(dependenciesPath)

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

    def isWarningVisible(self, module):
        BOOL_ENCODING = { False: 'false', True: 'true' }
        return BOOL_ENCODING[not module]

    def isPillowInstalled(self):
        # The Pillow module needs to be isolated to its own subprocess because many
        # distributions are prone to deadlock.
        importPath = os.path.join(self.addonFolder, 'import.py')
        try:
            subprocess.check_call([sys.executable, importPath])
            return True
        except subprocess.CalledProcessError:
            return False

    def generateSettings(self):
        xbmc.log('Generating default addon settings')
        root = xml.etree.ElementTree.Element('settings')
        dependencies = xml.etree.ElementTree.SubElement(root, 'category', {'label': '30018'})
        defaults = self.getDefaults()

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
            'visible': self.isWarningVisible(psutil) })
        xml.etree.ElementTree.SubElement(dependencies, 'setting', {
            'type': 'lsep',
            'label': '30023',
            'visible': self.isWarningVisible(alsaaudio) })
        xml.etree.ElementTree.SubElement(dependencies, 'setting', {
            'type': 'lsep',
            'label': '30024',
            'visible': self.isWarningVisible(pylirc) })
        xml.etree.ElementTree.SubElement(dependencies, 'setting', {
            'type': 'lsep',
            'label': '30026',
            'visible': self.isWarningVisible(self.isPillowInstalled()) })

        tree = xml.etree.ElementTree.ElementTree(root)
        settingsPath = os.path.join(self.addonFolder, 'resources/settings.xml')
        tree.write(settingsPath, encoding='utf-8', xml_declaration=True)


def main():
    RemoteControlBrowserService().generateSettings()


if __name__ == "__main__":
    main()

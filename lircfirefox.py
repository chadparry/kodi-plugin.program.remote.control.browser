#!/usr/bin/env python
# -*- coding: UTF-8 -*-
# vim: ts=4 sts=4 sw=4 tw=79 sta et
"""
An interface script for using lirc with Firefox, requires pylirc and xdotool
"""

__author__  = 'Patrick Butler'
__email__   = 'pbutler at killertux org'
__license__ = "GPLv2"

import alsaaudio
import pylirc
import subprocess
import sys
import time

VOLUME_MIN = 0L
VOLUME_MAX = 100L
VOLUME_DEFAULT = 50L
VOLUME_STEP = 5L

def main(args):
    """
    Fires off firefox, then inits pylirc and waits for remote presses
    """
    xstatus = subprocess.Popen(["xset", "-q"], stdout = subprocess.PIPE)
    xstatus.wait()
    has_dpms = any("DPMS is Enabled" in line for line in xstatus.stdout)
    if has_dpms:
        subprocess.Popen(["xset", "-dpms"])
    try:
        ffox(args)
    finally:
        if has_dpms:
            subprocess.Popen(["xset", "+dpms"])

def ffox(args):
    ffox = subprocess.Popen(["/usr/bin/firefox"] + args[1:])
    mixer = alsaaudio.Mixer()
    mixer.setvolume(VOLUME_DEFAULT)
    try:
        if not pylirc.init("firefox", "~/.lircrc", 1):
            return "Failed"
        stop = False
        while not stop:
            codes = pylirc.nextcode(1)
            if codes is None:
                continue
            for code in codes:
                #print code
                if code is None:
                    continue
                config = code["config"].split()
                if config[0] == "EXIT":
                    stop = True
                    break
                if config[0] == "VOLUME_UP":
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
                    mute = bool(mixer.getmute()[0])
                    mute = not mute
                    mixer.setmute(long(mute))
                if config[0] == "mousemove_relative":
                    mousestep = min(code["repeat"], 10)
                    config[2] = str(int(config[2]) * mousestep ** 2)
                    config[3] = str(int(config[3]) * mousestep ** 2)
                subprocess.Popen(["xdotool"] + config)
    except KeyboardInterrupt:
        print "Exiting...."
    p1 = subprocess.Popen(["xdotool", "search", "--title", "Mozilla Firefox"], stdout = subprocess.PIPE)
    p1.wait()
    windows = p1.stdout.readline().split()
    for window in windows:
        #print "'%s'" % window
        subprocess.Popen(["xdotool", "windowfocus", str(window)])
        subprocess.Popen(["xdotool", "key", "ctrl+q"])

    # If we found windows and they're still running, wait 3 seconds
    if len(windows) != 0 and ffox.poll() is None:
        for i in range(30):
            time.sleep(.1)
            if ffox.poll() is not None:
                break
    # Okay now we can forcibly kill it
    if ffox.poll() is None:
        ffox.terminate()

    mixer.setmute(long(False))
    mixer.setvolume(VOLUME_MAX)

    return 0

if __name__ == "__main__":
    sys.exit( main( sys.argv ) )

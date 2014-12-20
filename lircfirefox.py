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
import os
import pylirc
import signal
import subprocess
import sys
import time

VOLUME_MIN = 0L
VOLUME_MAX = 100L
VOLUME_DEFAULT = 50L
VOLUME_STEP = 2L
REPEAT_DELAY = 1

#log = open('/home/mythtv/log.txt', 'w')
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
    ffox = subprocess.Popen(args[1:])
    mixer = alsaaudio.Mixer()
    lastvolume = mixer.getvolume()[0]
    mute = lastvolume == 0
    repeat_keys = None
    repeat_index = None
    repeat_deadline = None
    try:
        if not pylirc.init("firefox", "~/.lircrc", 1):
            return "Failed"
        stop = False
        while not stop:
            codes = pylirc.nextcode(1)
            if codes is None:
                continue
            for code in codes:
                #print >>log, code
                if code is None:
                    continue
                config = code["config"].split()
                repeat = code["repeat"]
                if config[0] == "EXIT":
                    stop = True
                    break
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
                    now = time.time()
                    #print >>log, now, repeat_deadline
                    if repeat_keys == keys and now <= repeat_deadline:
                        config.append('BackSpace')
                        repeat_index += 1
                        current = keys[repeat_index % len(keys)]
                        config.append(current)
                    else:
                        repeat_keys = keys
                        repeat_index = 0
                        config.append(keys[0])
                    repeat_deadline = now + REPEAT_DELAY
                if config[0] == "mousemove_relative":
                    mousestep = min(repeat, 10)
                    config[2] = str(int(config[2]) * mousestep ** 2)
                    config[3] = str(int(config[3]) * mousestep ** 2)
                #print >>log, ["xdotool"] + config
                subprocess.Popen(["xdotool"] + config)
                #log.flush()
    except KeyboardInterrupt:
        #print >>log, "Exiting...."

    # Locate child processes
    ps_command = subprocess.Popen(["ps", "-o", "pid", "--ppid", str(ffox.pid), "--noheaders"], stdout = subprocess.PIPE)
    ps_output = ps_command.stdout.read()
    ps_command.wait()
    children = map(int, ps_output.split("\n")[:-1])

    sending_quit = subprocess.Popen(["xdotool", "search", "--pid", str(ffox.pid), "DUMMY", "key", "alt+F4"])
    sent_quit = not sending_quit.wait()

    # If we found windows and they're still running, wait 3 seconds
    if sent_quit and ffox.poll() is None:
        for i in range(30):
            time.sleep(.1)
            if ffox.poll() is not None:
                break
    # Okay now we can forcibly kill it
    try:
        ffox.terminate()
    except OSError:
        pass
    # Sometimes a zombie Flash process sticks around
    #print >>log, "Killing children: %s" % children
    for child in children:
        try:
            os.kill(child, signal.SIGTERM)
        except OSError:
            pass

    return 0

if __name__ == "__main__":
    sys.exit( main( sys.argv ) )

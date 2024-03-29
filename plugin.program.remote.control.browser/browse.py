#!/usr/bin/env python

import argparse
import collections
import contextlib
import datetime
import errno
import logging
import os
import pipes
import select
import shlex
import signal
import socket
import subprocess
import sys
import threading


logger = logging.getLogger('remotecontrolbrowser')
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)


# If any of these packages are missing, the script will attempt to proceed
# without those features.
try:
    import alsaaudio
except ImportError:
    logger.debug('Missing Python package: alsaaudio')
    alsaaudio = None
try:
    import psutil
except ImportError:
    logger.debug('Missing Python package: psutil')
    psutil = None
try:
    import pulsectl
except ImportError:
    logger.debug('Missing Python package: pulsectl')
    pulsectl = None
try:
    import pylirc
except ImportError:
    logger.debug('Missing Python package: pylirc')
    pylirc = None


VOLUME_MIN = 0
VOLUME_MAX = 100
DEFAULT_VOLUME = 50
DEFAULT_VOLUME_STEP = 1
RELEASE_KEY_DELAY = datetime.timedelta(seconds=1)
BROWSER_EXIT_DELAY = datetime.timedelta(seconds=3)


PylircCode = collections.namedtuple('PylircCode', ('config', 'repeat'))


class AlsaMixer(object):
    """Mixer that wraps ALSA"""

    def __init__(self, alsaControl):
        if alsaaudio is None:
            logger.debug('Not initializing an alsaaudio mixer')
            self.delegate = None
        else:
            try:
                self.delegate = alsaaudio.Mixer(alsaControl)
            except alsaaudio.ALSAAudioError as e:
                logger.info('Failed to initialize alsaaudio: ' + str(e))
                self.delegate = None

        if self.delegate is None:
            volume = DEFAULT_VOLUME
        else:
            channels = self.delegate.getvolume()
            volume = next(iter(channels))
            logger.debug('Detected initial volume: ' + str(volume))
        self.mute = not volume
        self.volume = volume or DEFAULT_VOLUME

    def _realizeVolume(self):
        if self.delegate is not None:
            # Muting the Master volume and then unmuting it is not a symmetric
            # operation, because other controls end up muted. So a mute needs
            # to be simulated by setting the volume level to zero.
            if self.mute:
                volume = 0
            else:
                volume = self.volume
            logger.debug('Setting volume: ' + str(volume))
            self.delegate.setvolume(volume)

    def toggleMute(self):
        self.mute = not self.mute
        self._realizeVolume()

    def incrementVolume(self):
        self.mute = False
        self.volume = min(self.volume + DEFAULT_VOLUME_STEP, VOLUME_MAX)
        self._realizeVolume()

    def decrementVolume(self):
        self.volume = max(self.volume - DEFAULT_VOLUME_STEP, VOLUME_MIN)
        self._realizeVolume()


class PulseMixer(object):
    """Mixer that wraps Pulse"""

    def __init__(self):
        if pulsectl is None:
            logger.debug('Not initializing a pulsectl mixer')
            self.pulse = None
            self.sink = None
        else:
            self.pulse = pulsectl.Pulse()
            self.sink = next(iter(self.pulse.sink_list()))

        if self.sink is None:
            volume = DEFAULT_VOLUME / 100.
        else:
            channels = self.sink.volume.values
            volume = self.sink.volume.value_flat
            logger.debug('Detected initial volume: ' + str(volume))
        self.mute = not volume
        self.volume = volume or DEFAULT_VOLUME / 100.

    def _realizeVolume(self):
        if self.sink is not None:
            # Muting the Master volume and then unmuting it is not a symmetric
            # operation, because other controls end up muted. So a mute needs
            # to be simulated by setting the volume level to zero.
            if self.mute:
                volume = 0
            else:
                volume = self.volume
            logger.debug('Setting volume: ' + str(volume))
            volume_buffer = self.sink.volume
            volume_buffer.value_flat = volume
            self.pulse.volume_set(self.sink, volume_buffer)

    def toggleMute(self):
        self.mute = not self.mute
        self._realizeVolume()

    def incrementVolume(self):
        self.mute = False
        self.volume = min(self.volume + DEFAULT_VOLUME_STEP / 100., 1.)
        self._realizeVolume()

    def decrementVolume(self):
        self.volume = max(self.volume - DEFAULT_VOLUME_STEP / 100., 0.)
        self._realizeVolume()


def terminateHandler(abortSocket):
    abortSocket.shutdown(socket.SHUT_RDWR)


@contextlib.contextmanager
def abortContext():
    (sink, source) = socket.socketpair()
    with contextlib.closing(sink), contextlib.closing(source):
        signal.signal(
            signal.SIGTERM,
            lambda signal, frame: terminateHandler(source))

        yield sink.fileno()


@contextlib.contextmanager
def suspendParentProcess(isEnabled):
    if not isEnabled:
        logger.debug('Not suspending Kodi')
        yield
        return
    parent = os.getppid()
    logger.info('Suspending Kodi')
    os.kill(parent, signal.SIGSTOP)
    try:
        yield
    finally:
        logger.info('Resuming Kodi')
        os.kill(parent, signal.SIGCONT)


@contextlib.contextmanager
def runPylirc(configuration):
    if pylirc is None:
        logger.debug('Not initializing pylirc')
        yield
        return
    logger.debug('Initializing pylirc with configuration: ' + configuration)
    fd = pylirc.init('browser', configuration)
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
        logger.debug('Not searching for process descendents')
        return [parent]
    try:
        process = psutil.Process(parent)
        try:
            children = process.children
        except AttributeError:
            children = process.get_children
        processes = [process] + children(recursive=True)
        return [node.pid for node in processes]
    except psutil.NoSuchProcess:
        logger.debug('Failed to find process tree')
        return []


def killBrowser(proc, sig):
    for pid in getProcessTree(proc.pid):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


@contextlib.contextmanager
def execBrowser(browserCmd):
    (sink, source) = socket.socketpair()
    with contextlib.closing(sink), contextlib.closing(source):
        waiter = None
        try:
            logger.info(
                'Launching browser: ' +
                ' '.join(pipes.quote(arg) for arg in browserCmd))
            proc = subprocess.Popen(browserCmd, close_fds=True)
            try:
                # Monitor the browser and kick the socket when it exits.
                waiterStarting = threading.Thread(
                    target=monitorProcess, args=(proc, source))
                waiterStarting.start()
                waiter = waiterStarting

                yield (proc, sink.fileno())

                # Ask each child process to exit.
                logger.debug('Terminating the browser')
                killBrowser(proc, signal.SIGTERM)

                # Give the browser a few seconds to shut down gracefully.
                def terminateBrowser():
                    logger.info(
                        'Forcibly killing the browser at the deadline')
                    killBrowser(proc, signal.SIGKILL)
                terminator = threading.Timer(
                    BROWSER_EXIT_DELAY.total_seconds(), terminateBrowser)
                terminator.start()
                try:
                    proc.wait()
                    proc = None
                    logger.debug('Waited for the browser to quit')
                finally:
                    terminator.cancel()
                    terminator.join()
            finally:
                if proc is not None:
                    # As a last resort, forcibly kill the browser.
                    logger.info('Forcibly killing the browser')
                    killBrowser(proc, signal.SIGKILL)
                    proc.wait()
                    logger.debug('Waited for the browser to die')
        finally:
            if waiter is not None:
                logger.debug(
                    'Joining with browser monitoring thread')
                waiter.join()
                logger.debug(
                    'Joined with browser monitoring thread')


def activateWindow(cmd, proc, isAborting, xdotoolPath):
    (output, _) = proc.communicate()
    if isAborting.is_set():
        logger.debug('Aborting search for browser PID')
        return
    if proc.returncode != 0:
        logger.info('Failed search for browser PID')
        raise subprocess.CalledProcessError(proc.returncode, cmd, output)
    wids = [wid for wid in output.split('\n') if wid]
    for wid in wids:
        if isAborting.is_set():
            logger.debug('Aborting activation of windows')
            return
        logger.debug('Activating window with WID: ' + wid)
        subprocess.call([xdotoolPath, 'WindowActivate', wid])
    logger.debug('Finished activating windows')


@contextlib.contextmanager
def raiseBrowser(pid, xdotoolPath):
    if xdotoolPath is None:
        logger.debug('Not raising the browser')
        yield
        return
    activator = None
    # With the "sync" flag, the command could block indefinitely.
    cmd = [xdotoolPath, 'search', '--sync', '--onlyvisible', '--pid', str(pid)]
    logger.info(
        'Searching for browser PID: ' +
        ' '.join(pipes.quote(arg) for arg in cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, universal_newlines=True)
    try:
        isAborting = threading.Event()
        activatorStarting = threading.Thread(
            target=activateWindow, args=(cmd, proc, isAborting, xdotoolPath))
        activatorStarting.start()
        activator = activatorStarting

        yield
    finally:
        isAborting.set()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if activator is not None:
            activator.join()


def driveBrowser(xdotoolPath, mixer, lircFd, browserExitFd, abortFd, parentFd):
    polling = [browserExitFd, abortFd, parentFd]
    if lircFd is not None:
        polling.append(lircFd)

    class CommandState:
        releaseKeyTime = None
        nextReleaseKeyTime = None
        isReleasing = False
        repeatKeys = None
        repeatIndex = 0
        isExiting = False

    def handleVolumeUpCommand(command, args, repeat):
        mixer.incrementVolume()
    def handleVolumeDownCommand(command, args, repeat):
        mixer.decrementVolume()
    def handleMuteCommand(command, args, repeat):
        mixer.toggleMute()
    def handleMultitapCommand(command, args, repeat):
        if CommandState.releaseKeyTime is not None and CommandState.repeatKeys == args:
            CommandState.repeatIndex += 1
        else:
            CommandState.isReleasing = True
            CommandState.repeatKeys = args
            CommandState.repeatIndex = 0
        CommandState.nextReleaseKeyTime = (datetime.datetime.now() + RELEASE_KEY_DELAY)
        current = args[CommandState.repeatIndex % len(args)]
        return ['key', '--clearmodifiers', '--', current, 'Shift+Left']
    def handleKeyCommand(command, args, repeat):
        CommandState.isReleasing = True
        return ['key', '--clearmodifiers', '--'] + args
    def handleClickCommand(command, args, repeat):
        CommandState.isReleasing = True
        # NOTE: XDOTOOL HACK
        # Some platforms include a buggy version of xdotool,
        # (https://github.com/jordansissel/xdotool/pull/102).
        # If you have version 3.20150503.1, then the mouse button
        # will not be released correctly. The workaround is to
        # uncomment the following line.
        #return ['click', '1']
        return ['click', '--clearmodifiers', '1']
    def handleMouseCommand(command, args, repeat):
        step = (repeat + 2) ** 2
        (horizontal, vertical) = args
        acceleratedHorizontal = str(int(horizontal) * step)
        acceleratedVertical = str(int(vertical) * step)
        return [
            'mousemove_relative',
            '--',
            acceleratedHorizontal,
            acceleratedVertical,
        ]
    def handleExitCommand(command, args, repeat):
        CommandState.isExiting = True
    def handleReleaseCommand(command, args, repeat):
        CommandState.isReleasing = True
    def handleUnrecognizedCommand(command, args, repeat):
        raise RuntimeError('Unrecognized LIRC config: ' + command)

    commandHandlers = {
        'VOLUME_UP': handleVolumeUpCommand,
        'VOLUME_DOWN': handleVolumeDownCommand,
        'MUTE': handleMuteCommand,
        'MULTITAP': handleMultitapCommand,
        'KEY': handleKeyCommand,
        'CLICK': handleClickCommand,
        'MOUSE': handleMouseCommand,
        'EXIT': handleExitCommand,
        'RELEASE': handleReleaseCommand,
    }

    while not CommandState.isExiting:
        if CommandState.releaseKeyTime is None:
            timeout = None
        else:
            timeout = max(
                (CommandState.releaseKeyTime - datetime.datetime.now()).total_seconds(),
                0)
        try:
            (rlist, _, _) = select.select(polling, [], [], timeout)
            if browserExitFd in rlist:
                logger.info('Exiting because the browser stopped prematurely')
                break
            if abortFd in rlist:
                logger.info('Exiting because a SIGTERM was received')
                break
            if parentFd in rlist:
                logger.info('Exiting because the parent has disappeared')
                break
            if lircFd is not None and lircFd in rlist:
                buttons = pylirc.nextcode(True)
            else:
                buttons = None
        except select.error as e:
            # Check whether this interrupt was from a signal to abort.
            if e[0] == errno.EINTR:
                (rlist, _, _) = select.select([abortFd], [], [], 0)
                if abortFd in rlist:
                    logger.info(
                        'Exiting because a SIGTERM interrupted a syscall')
                    break
            raise
        codes = ([PylircCode(**button) for button in buttons]
                 if buttons else [])
        if (CommandState.releaseKeyTime is not None and
                CommandState.releaseKeyTime <= datetime.datetime.now()):
            codes.append(PylircCode(config='RELEASE', repeat=0))

        for code in codes:
            logger.debug('Received LIRC code: ' + str(code))
            tokens = shlex.split(code.config)
            (command, args) = (tokens[0], tokens[1:])
            CommandState.isReleasing = False
            CommandState.nextReleaseKeyTime = None

            handler = commandHandlers.get(command, handleUnrecognizedCommand)
            inputs = handler(command, args, code.repeat)

            if CommandState.isExiting:
                break

            if CommandState.isReleasing and CommandState.releaseKeyTime is not None:
                if xdotoolPath is not None:
                    # Deselect the current multi-tap character.
                    logger.debug('Executing xdotool for multi-tap release')
                    subprocess.check_call(
                        [xdotoolPath, 'key', '--clearmodifiers', 'Right'])
                else:
                    logger.debug('Ignoring xdotool for multi-tap release')
            CommandState.releaseKeyTime = CommandState.nextReleaseKeyTime

            if inputs is not None:
                if xdotoolPath is not None:
                    cmd = [xdotoolPath] + inputs
                    logger.debug(
                        'Executing: ' +
                        ' '.join(pipes.quote(arg) for arg in cmd))
                    subprocess.check_call(cmd)
                else:
                    logger.debug('Ignoring xdotool inputs: ' + str(inputs))


def wrapBrowser(browserCmd, suspendKodi, lircConfig, xdotoolPath, alsaControl):
    mixer = PulseMixer() if alsaControl is None else AlsaMixer(alsaControl)
    with (
            abortContext()) as abortFd, (
            suspendParentProcess(suspendKodi)), (
            runPylirc(lircConfig)) as lircFd, (
            execBrowser(browserCmd)) as (browser, browserExitFd), (
            raiseBrowser(browser.pid, xdotoolPath)):
        driveBrowser(xdotoolPath, mixer, lircFd, browserExitFd, abortFd, sys.stdin)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suspend-kodi', action='store_true')
    parser.add_argument('--lirc-config', required=True)
    parser.add_argument('--xdotool-path')
    parser.add_argument('--alsa-control')
    parser.add_argument('cmd', nargs='+')
    args = parser.parse_args()

    wrapBrowser(
        args.cmd,
        args.suspend_kodi,
        args.lirc_config,
        args.xdotool_path,
        args.alsa_control)


if __name__ == "__main__":
    main()

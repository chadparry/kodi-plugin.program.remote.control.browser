#!/bin/bash

# Prevent loading two or more tabs due to LIRC still being enabled in XBMC / KODI
CHROME_STARTED=`ps -ef | grep google | grep chrome | grep -v "grep" | wc -l`
if [ $CHROME_STARTED -gt 0 ]; then
	exit 1;
fi

/usr/bin/google-chrome "$@" &

exit 0

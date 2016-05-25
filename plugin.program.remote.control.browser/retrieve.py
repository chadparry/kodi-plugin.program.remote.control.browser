#!/usr/bin/python
# -*- coding: utf-8 -*-
import io
import PIL.Image
import PIL.PngImagePlugin
import sys
import urllib2

url = sys.argv[1]
filename = sys.argv[2]

favicon = urllib2.urlopen(url)
buffered = io.BytesIO(favicon.read())
PIL.Image.open(buffered).verify()
# The image must be re-opened after verification.
buffered.seek(0)
image = PIL.Image.open(buffered)
image.save(filename, PIL.PngImagePlugin.PngImageFile.format)

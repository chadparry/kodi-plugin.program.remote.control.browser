#!/usr/bin/python
# -*- coding: utf-8 -*-
import argparse
import io
import PIL.Image
import PIL.PngImagePlugin
import sys
import urllib2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    parser.add_argument('filename', type=argparse.FileType('w'))
    args = parser.parse_args()

    download = urllib2.urlopen(args.url)
    buffered = io.BytesIO(download.read())
    PIL.Image.open(buffered).verify()
    # The image must be re-opened after verification.
    buffered.seek(0)
    image = PIL.Image.open(buffered)
    image.save(args.filename, PIL.PngImagePlugin.PngImageFile.format)


if __name__ == "__main__":
    main()

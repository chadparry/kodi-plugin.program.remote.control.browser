import argparse
import io
import os
import PIL.Image
import PIL.PngImagePlugin
import shutil
import tempfile
import urllib2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    parser.add_argument('filename')
    args = parser.parse_args()

    download = urllib2.urlopen(args.url)
    buffered = io.BytesIO(download.read())
    PIL.Image.open(buffered).verify()
    # The image must be re-opened after verification.
    buffered.seek(0)
    image = PIL.Image.open(buffered)
    with tempfile.NamedTemporaryFile(delete=False) as saved:
        image.save(saved, PIL.PngImagePlugin.PngImageFile.format)

    # Atomically move to the desired location. (This is only atomic if the
    # temp directory is on the same filesystem).
    try:
        shutil.move(saved.name, args.filename)
    except OSError:
        try:
            os.remove(saved.name)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    main()

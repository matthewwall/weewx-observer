# installer for the weewx-observer driver
# Copyright 2017 Matthew Wall, all rights reserved

from setup import ExtensionInstaller

def loader():
    return ObserverInstaller()

class ObserverInstaller(ExtensionInstaller):
    def __init__(self):
        super(ObserverInstaller, self).__init__(
            version="0.2",
            name='observer',
            description='Capture data from observer weather station',
            author="Matthew Wall",
            author_email="mwall@users.sourceforge.net",
            files=[('bin/user', ['bin/user/observer.py'])]
            )

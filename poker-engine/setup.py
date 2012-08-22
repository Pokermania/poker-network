#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
from distutils.core import setup

from distutils.command.build import build as DistutilsBuild

class ExtendedBuild(DistutilsBuild):
    
    def run(self):
        DistutilsBuild.run(self)
        os.system("make -C po all")
        os.system("make -C conf buildconf")

setup(
    name = 'poker-engine',
    version = '1.4.5',
    packages = ['pokerengine'],
    cmdclass = {'build': ExtendedBuild}
)


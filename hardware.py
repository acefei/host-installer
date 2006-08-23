#!/usr/bin/env python
# Copyright (c) 2005-2006 XenSource, Inc. All use and distribution of this 
# copyrighted material is governed by and subject to terms and conditions 
# as licensed by XenSource, Inc. All other rights reserved.
# Xen, XenSource and XenEnterprise are either registered trademarks or 
# trademarks of XenSource Inc. in the United States and/or other countries.

###
# XEN CLEAN INSTALLER
# Hardware discovery tools
#
# written by Andrew Peace

import os
import version

# More of the hardware tools will be moved into here in future.

# module => module list
# if we discover x, actually load module_map[x]:
module_map = {
    # general:
    'mptscsih'     : ['mptspi', 'mptscsih'],
    'i810-tco'     : [],
    'usb-uhci'     : [],
    'ide-scsi'     : ['ide-generic'],
    'piix'         : ['ata-piix', 'piix'],

    # blacklist framebuffer drivers (we don't need them):
    "arcfb"        : [],
    "aty128fb"     : [],
    "atyfb"        : [],
    "radeonfb"     : [],
    "cirrusfb"     : [],
    "cyber2000fb"  : [],
    "cyblafb"      : [],
    "gx1fb"        : [],
    "hgafb"        : [],
    "i810fb"       : [],
    "intelfb"      : [],
    "kyrofb"       : [],
    "i2c-matroxfb" : [],
    "neofb"        : [],
    "nvidiafb"     : [],
    "pm2fb"        : [],
    "rivafb"       : [],
    "s1d13xxxfb"   : [],
    "savagefb"     : [],
    "sisfb"        : [],
    "sstfb"        : [],
    "tdfxfb"       : [],
    "tridentfb"    : [],
    "vfb"          : [],
    "vga16fb"      : [],
    }


###
# Module loading and order retrieval

__MODULE_ORDER_FILE__ = "/tmp/module-order"

class ModuleOrderUnknownException(Exception):
    pass

def getModuleOrder():
    def allKoFiles(directory):
        kofiles = []
        items = os.listdir(directory)
        for item in items:
            if item.endswith(".ko"):
                kofiles.append(item)
            itemabs = os.path.join(directory, item)
            if os.path.isdir(itemabs):
                kofiles.extend(allKoFiles(itemabs))

        return kofiles

    try:
        all_modules = allKoFiles("/lib/modules/%s" % version.KERNEL_VERSION)
        all_modules = [x.replace(".ko", "") for x in all_modules]

        mo = open(__MODULE_ORDER_FILE__, 'r')
        lines = [x.strip() for x in mo]
        mo.close()

        modules = []
        for module in lines:
            if module in all_modules:
                modules.append(module)
            else:
                module = module.replace("-", "_")
                if module in all_modules:
                    modules.append(module)
        
        return modules
    except Exception, e:
        raise ModuleOrderUnknownException, e

def readCpuInfo():
    f = open('/proc/cpuinfo', 'r')
    cpus = []
    cpu = {}
    for line in f:
        if line == "\n":
            cpus.append(cpu)
            cpu = {}
        else:
            (key, value) = line.split(":")
            (key, value) = (key.strip(), value.strip())
            if key == "flags":
                value = value.split(" ")
            cpu[key] = value
    f.close()
    
    return cpus

# to explain this monster:
# - flags is a list of lists containing the flags for each CPU
# - support is a list of Bool, saying whether one of 'features'
#   is contained in the equivalent 'flags' entry
# - vt checks that the required features rae present on at least
#   one CPU.  They will, in reality, be present on all or none.
def VTSupportEnabled():
    features = ['vmx', 'svm']
    cpuinfo = readCpuInfo()
    flags = [ x['flags'] for x in cpuinfo ]

    support = [True in [x in f for x in features] for f in flags]
    vt = reduce(lambda x,y: x or y, support)
    
    return vt
    

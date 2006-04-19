#!/usr/bin/python
#
# Copyright (c) 2005 XenSource, Inc. All use and distribution of this
# copyrighted material is governed by and subject to terms and 
# conditions as licensed by XenSource, Inc. All other rights reserved. 
#

import os
import os.path
import sys
import commands
import p2v_constants
import p2v_utils
import p2v_tui

from p2v_error import P2VError

ui_package = p2v_tui


def run_command(cmd):
#    p2v_utils.trace_message("running: %s\n" % cmd)
    rc, out = commands.getstatusoutput(cmd)
    if rc != 0:
        p2v_utils.trace_message("Failed %d: %s\n" % (rc, out))
    return (rc, out)

def parse_blkid(line):
    """Take a line of the form '/dev/foo: key="val" key="val" ...' and return
a dictionary created from the key/value pairs and the /dev entry as 'path'"""

    dev_attrs = {}
    i =  line.find(":")
    dev_attrs[p2v_constants.DEV_ATTRS_PATH] = line[0:i]
    attribs = line[i+1:].split(" ")
    for attr in attribs:
        if len(attr) == 0:
            continue
        name, val = attr.split("=")
        dev_attrs[name.lower()] = val.strip('"')
    return dev_attrs
    
def scan():
    devices = {}
    
    #activate LVM
    run_command("vgscan")
    run_command("vgchange -a y")
    rc, out = run_command("/sbin/blkid -c /dev/null")
    if rc == 0 and out:
        for line in out.split("\n"):
            attrs = parse_blkid(line)
            devices[attrs[p2v_constants.DEV_ATTRS_PATH]] = attrs
    else:
        raise P2VError("Failed to scan devices")
    return devices

def mount_dev(dev, dev_type, mntpnt, options):
    umount_dev(mntpnt) # just a precaution, don't care if it fails
    rc, out = run_command("echo 1 > /proc/sys/kernel/printk")
    rc, out = run_command("mount -o %s -t %s %s %s %s" % (options, dev_type,
                                                       dev, mntpnt, p2v_utils.show_debug_output()))
    return rc

def umount_dev(mntpnt):
    rc, out = run_command("umount %s" % (mntpnt))
    return rc

def umount_all_dev(devices):
    fp = open("/proc/mounts")
    mounts = load_fstab(fp)
    for dev_name, dev_attrs in devices.items():
        candidates = [ x[0] for x in mounts.keys() if x[1] == dev_name ]
        if not len(candidates):
            continue
        assert(len(candidates) == 1)
        umount_dev(candidates[0])

def load_fstab(fp):
    fstab = {}
    for line in fp.readlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        pieces = line.split()
        if ',' in pieces[3]:
            pieces[3] = [ x.strip() for x in pieces[3].split(',') ]
        fstab[(pieces[1], pieces[0])] = pieces
    return fstab

def find_dev_for_label(devices, label):
    for value in devices.values():
        if value.has_key(p2v_constants.DEV_ATTRS_LABEL) and value[p2v_constants.DEV_ATTRS_LABEL] == label:
            return value[p2v_constants.DEV_ATTRS_PATH]
    return None

def find_extra_mounts(fstab, devices):
    import copy
    
    mounts = []
    for ((mntpnt, dev), info) in fstab.items():
        if info[2] not in ('ext2', 'ext3', 'reiserfs') or \
           mntpnt == '/' or \
           'noauto' in info[3]:
            continue

        mount_info = copy.deepcopy(info)

        # convert label to real device name
        if 'LABEL=' in info[0]:
            label = mount_info[0][6:]
            mount_info[0] = find_dev_for_label(devices, label)

        options = None
        if type(mount_info[3]) == type([]):
            mount_info[3] = ','.join(filter(lambda x: not (x == "rw" or \
                                                           x == "ro"),
                                            mount_info[3]))

        mounts.append(mount_info)
    return mounts

def determine_size(mntpnt, dev_name):
    fp = open(os.path.join(mntpnt, 'etc', 'fstab'))
    fstab = load_fstab(fp)
    fp.close()
    
    devices = scan()

    active_mounts = []
    p2v_utils.trace_message("* Need to mount:")
    mounts = find_extra_mounts(fstab, devices)
    for mount_info in mounts:
#        p2v_utils.trace_message("  --", mount_info)
        extra_mntpnt = os.path.join(mntpnt, mount_info[1][1:])

        rc = mount_dev(mount_info[0], mount_info[2],
                       extra_mntpnt, mount_info[3] + ",ro")
                       
        if rc != 0:
            raise P2VError("Failed to determine size - mount failed.")

        active_mounts.append(extra_mntpnt)

    # get the used size
    command = "df -k | grep %s | awk '{print $3}'" % mntpnt
    p2v_utils.trace_message("going to run : %s" % command)
    rc, used_out = run_command(command);
    if rc != 0:
        raise P2VError("Failed to determine used size - df failed")

    #get the total size
    command = "df -k | grep %s | awk '{print $2}'" % mntpnt
    p2v_utils.trace_message("going to run : %s" % command)
    rc, total_out = run_command(command);
    if rc != 0:
        raise P2VError("Failed to determine used size - df failed")
    
    p2v_utils.trace_message("\n\nFS used Usage : %s, FS total usage : %s\n" % (used_out, total_out))
    used_size = long(0)
    total_size = long(0)
    
    split_used_size = used_out.split('\n')
    split_total_size = total_out.split('\n')
    for o in split_used_size:
        p2v_utils.trace_message("\n\nFS used Usage : %s\n\n" % o)
        used_size += int(o)
    for o in split_total_size:
        p2v_utils.trace_message("\n\nFS total Usage : %s\n\n" % o)
        total_size += int(o)
        
    p2v_utils.trace_message("\n\nFinal FS used Usage : %d\n\n" % used_size)
    p2v_utils.trace_message("\n\nFinal FS total Usage : %d\n\n" % total_size)
    
    for item in active_mounts:
        # assume the umount works
        umount_dev(item)

    return str(used_size), str(total_size)


def handle_root(mntpnt, dev_name, pd = None):
    rc = 0
    fp = open(os.path.join(mntpnt, 'etc', 'fstab'))
    fstab = load_fstab(fp)
    fp.close()
    
    ui_package.displayProgressDialog(0, pd, " - Scanning and mounting devices")
                                       
    devices = scan()
    
    active_mounts = []
    p2v_utils.trace_message("* Need to mount:")
    mounts = find_extra_mounts(fstab, devices)
    for mount_info in mounts:
        #p2v_utils.trace_message("  --", mount_info)
        extra_mntpnt = os.path.join(mntpnt, mount_info[1][1:])

        rc = mount_dev(mount_info[0], mount_info[2],
                       extra_mntpnt, mount_info[3] + ",ro")
        if rc != 0:
            raise P2VError("Failed to handle root - mount failed.")

        active_mounts.append(extra_mntpnt)

    ui_package.displayProgressDialog(1, pd, " - Compressing root filesystem")

    hostname = findHostName(mntpnt)
    os.chdir(mntpnt)
    tar_basefilename = "p2v%s.%s.tar.bz2" % (hostname, os.path.basename(dev_name))
    base_dirname = "/xenpending/"
    tar_filename = "%s%s" % (base_dirname, tar_basefilename)
    rc, out = run_command("tar cjvf %s . %s" % (tar_filename, p2v_utils.show_debug_output()))
    if not rc == 0:
        raise P2VError("Failed to handle root - tar failed with %d ( out = %s ) " % rc, out)
    
    ui_package.displayProgressDialog(2, pd, " - Calculating md5sum")
    rc, md5_out = run_command("md5sum %s | awk '{print $1}'" % tar_filename)
    if rc != 0:
        raise P2VError("Failed to handle root - md5sum failed")
    os.chdir("/")

    for item in active_mounts:
        # assume the umount works
        umount_dev(item)

    return (0, base_dirname, tar_basefilename, md5_out)

def mount_os_root(dev_name, dev_attrs):
    mntbase = "/var/mnt"
    mnt = mntbase + "/" + os.path.basename(dev_name)
    rc, out = run_command("mkdir -p %s" % (mnt))
    if rc != 0:
        p2v_utils.trace_message("mkdir failed\n")
        raise P2VError("Failed to mount os root - mkdir failed")
    
    rc = mount_dev(dev_name, dev_attrs['type'], mnt, 'ro')
#    if rc != 0:
#       raise P2VError("Failed to mount os root")
    return mnt

def findHostName(mnt):
    hostname = "localhost"
    hnFile = os.path.join(mnt,'etc', 'hostname')
    if os.path.exists(hnFile):
        hn = open(hnFile)
        for line in hn.readlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            hostname = line
            break
    return hostname
    
def inspect_root(dev_name, dev_attrs, results):
    mnt = mount_os_root(dev_name, dev_attrs)
    if os.path.exists(os.path.join(mnt, 'etc', 'fstab')):
       p2v_utils.trace_message("* Found root partition on %s" % dev_name)
       rc, out = run_command("/opt/xensource/clean-installer/read_osversion.sh " + mnt)
       if rc == 0:
           p2v_utils.trace_message("read_osversion succeeded : out = %s" % out)
           parts = out.split('\n')
           if len(parts) > 0:
               os_install = {}
               p2v_utils.trace_message("found os name: %s" % parts[0])
               p2v_utils.trace_message("found os version : %s" % parts[1])
               
               os_install[p2v_constants.OS_NAME] = parts[0]
               os_install[p2v_constants.OS_VERSION] = parts[1]
               os_install[p2v_constants.DEV_NAME] = dev_name
               os_install[p2v_constants.DEV_ATTRS] = dev_attrs
               os_install[p2v_constants.HOST_NAME] = findHostName(mnt)
               results.append(os_install)
       else:
           p2v_utils.trace_message("read_osversion failed : out = %s" % out)
           raise P2VError("Failed to inspect root - read_osversion failed.")
    umount_dev(mnt)

def findroot():
    devices = scan()
    results = []

    for dev_name, dev_attrs in devices.items():
        if dev_attrs.has_key(p2v_constants.DEV_ATTRS_TYPE) and dev_attrs[p2v_constants.DEV_ATTRS_TYPE] in ('ext2', 'ext3', 'reiserfs'):
            inspect_root(dev_name, dev_attrs, results)
                   
    #run_command("sleep 2")
    return results

def create_xgt(xgt_create_dir, xgt_filename, template_filename, tar_filename):
    #command = "tar cfv %s/%s -C %s %s %s" % (xgt_create_dir, xgt_filename, xgt_create_dir, template_filename, tar_filename)
    command = "cd %s && zip %s %s %s" % (xgt_create_dir, xgt_filename, template_filename, tar_filename)
    rc, out = run_command(command)
    if rc != 0:
        raise P2VError("Failed to create xgt - zip failed")
    return

def get_mem_info():
    command = "cat /proc/meminfo | grep MemTotal | awk '{print $2}'"
    rc, out = run_command(command)
    if rc != 0:
        raise P2VError("Failed to get mem size")
    return out

def get_cpu_count():
    command = "cat /proc/cpuinfo | grep processor | wc -l"
    rc, out = run_command(command)
    if rc != 0:
        raise P2VError("Failed to get cpu count")
    return out

if __name__ == '__main__':
    mntbase = "/var/mnt"

    devices = scan()

    umount_all_dev(devices)

    for dev_name, dev_attrs in devices.items():
        if dev_attrs.has_key(p2v_constants.DEV_ATTRS_TYPE) and dev_attrs[p2v_constants.DEV_ATTRS_TYPE] in ('ext2', 'ext3', 'reiserfs'):
            mnt = mntbase + "/" + os.path.basename(dev_name)
            rc, out = run_command("mkdir -p %s" % (mnt))
            if rc != 0:
                p2v_utils.trace_message("mkdir failed\n")
                sys.exit(1)

            rc = mount_dev(dev_name, dev_attrs[p2v_constants.DEV_ATTRS_TYPE], mnt, 'ro')
            if rc != 0:
                p2v_utils.trace_message("Failed to mount mnt")
                continue
                #sys.exit(rc)

            if os.path.exists(os.path.join(mnt, 'etc', 'fstab')):
                p2v_utils.trace_message("* Found root partition on %s" % dev_name)
                rc, tar_dirname, tar_filename, md5sum = handle_root(mnt, dev_name)
                if rc != 0:
                    p2v_utils.trace_message("%s failed\n" % dev_name)
                    sys.exit(rc)

            umount_dev(mnt)

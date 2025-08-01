# SPDX-License-Identifier: GPL-2.0-only

import datetime
import os.path
import time
import functools

import generalui
from uicontroller import SKIP_SCREEN, EXIT, LEFT_BACKWARDS, RIGHT_FORWARDS, REPEAT_STEP
import constants
import diskutil
from disktools import *
from version import *
from xcp import logger
import snackutil
import util
import socket
import product
import upgrade
import netutil
import dmvutil

from snack import *

import tui
import tui.network
import tui.progress
import driver

from netinterface import NetInterface

dmv_data_provider = None

MY_PRODUCT_BRAND = PRODUCT_BRAND or PLATFORM_NAME

def selectDefault(key, entries):
    """ Given a list of (text, key) and a key to select, returns the appropriate
    text,key pair, or None if not in entries. """

    for text, k in entries:
        if key == k:
            return text, k
    return None

# welcome screen:
def welcome_screen(answers):
    driver_answers = {'driver-repos': []}

    if constants.HAS_DRIVER_DISKS:
        tui.update_help_line([None, "<F9> load driver"])

    def load_driver(driver_answers):
        tui.screen.popHelpLine()
        tui.update_help_line([None, ' '])
        drivers = driver.doInteractiveLoadDriver(tui, driver_answers)
        logger.log(drivers)
        logger.log(driver_answers)
        if drivers[0]:
            if 'extra-repos' not in answers: answers['extra-repos'] = []
            answers['extra-repos'].append(drivers)
        return True

    global loop
    loop = True

    def fn9():
        global loop
        loop = True
        return False

    while loop:
        loop = False
        driver_answers['network-hardware'] = answers['network-hardware'] = netutil.scanConfiguration()
        welcome_text = """This setup tool can be used to install or upgrade %s on your system or restore your server from backup.  Installing %s will erase all data on the disks selected for use.

Please make sure you have backed up any data you wish to preserve before proceeding.
""" % (MY_PRODUCT_BRAND, MY_PRODUCT_BRAND)
        if constants.HAS_DRIVER_DISKS:
            welcome_text += "\nTo load a device driver press <F9>.\n"

        hotkeys = {}
        if constants.HAS_DRIVER_DISKS:
            hotkeys = {'F9': fn9}

        button = snackutil.ButtonChoiceWindowEx(tui.screen,
                                "Welcome to %s Setup" % MY_PRODUCT_BRAND,
                                welcome_text,
                                ['Ok', 'Reboot'], width=60, help="welcome",
                                hotkeys=hotkeys)
        if loop:
            load_driver(driver_answers)
            tui.update_help_line([None, "<F9> load driver"])

    tui.screen.popHelpLine()

    if button == 'reboot':
        return EXIT

    logger.log("Waiting for partitions to appear...")
    util.runCmd2(util.udevsettleCmd())
    time.sleep(1)
    diskutil.mpath_part_scan()

    # ensure partitions/disks are not locked by LVM
    lvm = LVMTool()
    lvm.deactivateAll()
    del lvm

    tui.progress.showMessageDialog("Please wait", "Checking for existing products...")
    answers['installed-products'] = product.find_installed_products()
    answers['upgradeable-products'] = upgrade.filter_for_upgradeable_products(answers['installed-products'])
    answers['backups'] = product.findXenSourceBackups()
    tui.progress.clearModelessDialog()

    diskutil.log_available_disks()

    # CA-41142, ensure we have at least one network interface and one disk before proceeding
    label = None
    if len(diskutil.getDiskList()) == 0:
        label = "No Disks"
        text = "hard disks"
        text_short = "disks"
    if len(answers['network-hardware'].keys()) == 0:
        label = "No Network Interfaces"
        text = "network interfaces"
        text_short = "interfaces"
    if label:
        text = """This host does not appear to have any %s.

If %s are present you may need to load a device driver on the previous screen for them to be detected.""" % (text, text_short)
        ButtonChoiceWindow(tui.screen, label, text, ["Back"], width=48)
        return REPEAT_STEP

    return RIGHT_FORWARDS

def hardware_warnings(answers, ram_warning, vt_warning):
    vt_not_found_text = "Hardware virtualization assist support is not available on this system.  Either it is not present, or is disabled in the system's BIOS.  This capability is required to start Windows virtual machines."
    not_enough_ram_text = "%s requires %dMB of system memory in order to function normally.  Your system appears to have less than this, which may cause problems during startup." % (MY_PRODUCT_BRAND, constants.MIN_SYSTEM_RAM_MB_RAW)

    text = "The following problem(s) were found with your hardware:\n\n"
    if vt_warning:
        text += vt_not_found_text + "\n\n"
    if ram_warning:
        text += not_enough_ram_text + "\n\n"
    text += "You may continue with the installation, though %s might have limited functionality until you have addressed these problems." % MY_PRODUCT_BRAND

    button = ButtonChoiceWindow(
        tui.screen,
        "System Hardware",
        text,
        ['Ok', 'Back'],
        width=60, help="hwwarn"
        )

    if button == 'back': return LEFT_BACKWARDS
    return RIGHT_FORWARDS

def overwrite_warning(answers):
    warning_string = "Continuing will result in a clean installation, all existing configuration will be lost."
    warning_string += "\n\nAlternatively, please contact a Technical Support Representative for the recommended upgrade path."

    button = snackutil.ButtonChoiceWindowEx(
        tui.screen,
        "Warning",
        ("Only product installations that cannot be upgraded have been detected.\n\n%s" % warning_string),
        ['Ok', 'Back'],
        width=60, help="overwrtwarn", default=1,
        )

    if button == 'back': return LEFT_BACKWARDS
    return RIGHT_FORWARDS

def get_admin_interface(answers):
    default = None
    try:
        if 'net-admin-interface' in answers:
            default = answers['net-admin-interface']
    except:
        pass

    net_hw = answers['network-hardware']

    direction, iface = tui.network.select_netif("Which network interface would you like to use for connecting to the management server on your host?",
                                                net_hw, False, default)
    if direction == RIGHT_FORWARDS:
        answers['net-admin-interface'] = iface
    return direction

def get_admin_interface_configuration(answers):
    if 'net-admin-interface' not in answers:
        answers['net-admin-interface'] = list(answers['network-hardware'].keys())[0]
    nic = answers['network-hardware'][answers['net-admin-interface']]

    defaults = None
    try:
        if 'net-admin-configuration' in answers:
            defaults = answers['net-admin-configuration']
        elif 'runtime-iface-configuration' in answers:
            all_dhcp, manual_config = answers['runtime-iface-configuration']
            if not all_dhcp:
                defaults = manual_config[answers['net-admin-interface']]
    except:
        pass

    rc, conf = tui.network.get_iface_configuration(
        nic, txt="Please specify how networking should be configured for the management interface on this host.",
        defaults=defaults
        )
    if rc == RIGHT_FORWARDS:
        answers['net-admin-configuration'] = conf
    return rc

def get_installation_type(answers):
    entries = []
    for x in answers['upgradeable-products']:
        entries.append(("Upgrade %s on %s" % (x, diskutil.getHumanDiskLabel(x.primary_disk, short=True)),
                        (x, x.settingsAvailable())))
    for b in answers['backups']:
        entries.append(("Restore %s from backup to %s" % (b, diskutil.getHumanDiskLabel(b.root_disk, short=True)),
                        (b, None)))

    entries.append( ("Perform clean installation", None) )

    # default value?
    if 'install-type' in answers and answers['install-type'] == constants.INSTALL_TYPE_REINSTALL:
        default = selectDefault(answers['installation-to-overwrite'], entries)
    elif 'install-type' in answers and answers['install-type'] == constants.INSTALL_TYPE_RESTORE:
        default = selectDefault(answers['backup-to-restore'], entries)
    else:
        default = None

    if len(answers['upgradeable-products']) > 0:
        text = "One or more existing product installations that can be upgraded have been detected."
        if len(answers['backups']) > 0:
            text += "  In addition one or more backups have been detected."
    else:
        text = "One or more backups have been detected."
    text += "\n\nWhat would you like to do?"

    tui.update_help_line([None, "<F5> more info"])

    def more_info(context):
        if not context: return True
        obj, _ = context
        if isinstance(obj, product.ExistingInstallation):
            use = "%s installation" % obj.visual_brand
        elif isinstance(obj, product.XenServerBackup):
            use = "%s backup" % obj.visual_brand
        else:
            return True

        date = "Unknown"
        if 'INSTALLATION_DATE' in obj.inventory:
            date = obj.inventory['INSTALLATION_DATE']
        dev = "Unknown"
        if 'PRIMARY_DISK' in obj.inventory:
            pd = obj.inventory['PRIMARY_DISK']
            if pd == "ToBeDetermined":
                dev = diskutil.getHumanDiskName(obj.primary_disk)
            else:
                dev = "%s (%s)" % (diskutil.getHumanDiskName(os.path.realpath(pd)),
                               diskutil.getHumanDiskName(pd))

        tui.update_help_line([' ', ' '])
        args = ([("Use:", use),
                 ("Version:", str(obj.visual_version))] +
                ([("Build:", str(obj.build))] if obj.build is not None else []) +
                [("Installed:", date),
                 ("Disk:", dev)])
        snackutil.TableDialog(tui.screen, "Details", *args)
        tui.screen.popHelpLine()
        return True

    (button, entry) = snackutil.ListboxChoiceWindowEx(
        tui.screen,
        "Action To Perform",
        text,
        entries,
        ['Ok', 'Back'], width=60, default=default, help='action:info',
        hotkeys={'F5': more_info})

    tui.screen.popHelpLine()

    if button == 'back':
        return LEFT_BACKWARDS

    if entry is None:
        answers['install-type'] = constants.INSTALL_TYPE_FRESH
        answers['preserve-settings'] = False

        if 'installation-to-overwrite' in answers:
            del answers['installation-to-overwrite']
    elif isinstance(entry[0], product.ExistingInstallation):
        answers['install-type'] = constants.INSTALL_TYPE_REINSTALL
        answers['installation-to-overwrite'], preservable = entry
        answers['preserve-settings'] = preservable
        if 'primary-disk' not in answers:
            answers['primary-disk'] = answers['installation-to-overwrite'].primary_disk

        for k in ['guest-disks', 'default-sr-uuid']:
            if k in answers:
                del answers[k]
    elif isinstance(entry[0], product.XenServerBackup):
        answers['install-type'] = constants.INSTALL_TYPE_RESTORE
        answers['backup-to-restore'], _ = entry

    return RIGHT_FORWARDS

def ha_master_upgrade(answers):
    button = ButtonChoiceWindow(
        tui.screen,
        "High Availability Enabled",
        """High Availability must be disabled before upgrade.

Please reboot this host, disable High Availability on the pool, check which server is the pool master and then restart the upgrade procedure.""",
        ['Cancel', 'Back'],
        width=60, help='hawarn'
        )

    if button == 'back': return LEFT_BACKWARDS
    return EXIT

def master_not_upgraded(answers):
    button = ButtonChoiceWindow(
        tui.screen,
        "Pool Master Version",
        "The master host of this pool must be upgraded before this slave.",
        ['Cancel', 'Back'],
        width=60, help='masterwarn'
        )

    if button == 'back': return LEFT_BACKWARDS
    return EXIT

def migrate_to_corosync3_check(answers):
    button = ButtonChoiceWindow(
        tui.screen,
        "Corosync 2 Is In Use",
        "This host uses version 2 of Corosync. This version is obsoleted by version 3 in the next XenServer release. Before upgrading, perform a manual migration from Corosync version 2 to Corosync version 3.",
        ['Cancel', 'Back'],
        width=60, help='corosync2'
        )

    if button == 'back': return LEFT_BACKWARDS
    return EXIT

def upgrade_settings_warning(answers):
    button = ButtonChoiceWindow(
        tui.screen,
        "Preserve Settings",
        """The configuration of %s cannot be automatically retained. You must re-enter the configuration manually.

Warning: You must use the current values. Failure to do so may result in an incorrect installation of the product.""" % str(answers['installation-to-overwrite']),
        ['Ok', 'Back'],
        width=60, help='preswarn'
        )

    if button == 'back': return LEFT_BACKWARDS
    return RIGHT_FORWARDS

def remind_driver_repos(answers):
    driver_list = []
    settings = answers['installation-to-overwrite'].readSettings()
    for repo in settings['repo-list']:
        pkid, name, is_supp = repo
        if is_supp and name not in driver_list and \
               pkid not in constants.INTERNAL_REPOS:
            driver_list.append(name)

    if len(driver_list) == 0:
        return SKIP_SCREEN

    text = ''
    for driver in driver_list:
        text += " * %s\n" % driver

    button = ButtonChoiceWindow(
        tui.screen,
        "Installed Supplemental Packs",
        """The following Supplemental Packs are present in the current installation:

%s
Please ensure that the functionality they provide is either included in the version of %s being installed or by a Supplemental Pack for this release.""" % (text, MY_PRODUCT_BRAND),
        ['Ok', 'Back'],
        width=60, help="suppackremind"
        )

    if button == 'back': return LEFT_BACKWARDS
    return RIGHT_FORWARDS

def repartition_existing(answers):
    button = ButtonChoiceWindow(
        tui.screen,
        "Convert Existing Installation",
        """The installer needs to change the disk layout of your existing installation.

The conversion will replace all previous system image partitions to create the %s disk partition layout.

Continue with installation?""" % (MY_PRODUCT_BRAND,),
        ['Continue', 'Back'], help='repartwarn'
        )
    if button == 'back': return LEFT_BACKWARDS

    answers['backup-existing-installation'] = True
    return RIGHT_FORWARDS

def force_backup_screen(answers):
    text = "The installer needs to create a backup of your existing installation. This will erase all data currently on the backup partition (including previous backups)."
    button = ButtonChoiceWindow(
        tui.screen,
        "Previous Installation Detected",
        text,
        ['Continue', 'Back'], width=60, help='forceback'
        )
    if button == 'back': return LEFT_BACKWARDS

    answers['backup-existing-installation'] = True
    return RIGHT_FORWARDS

def backup_existing_installation(answers):
    # default selection:
    if 'backup-existing-installation' in answers:
        if answers['backup-existing-installation']:
            default = 0
        else:
            default = 1
    else:
        default = 0

    button = snackutil.ButtonChoiceWindowEx(
        tui.screen,
        "Back-up Existing Installation?",
        """Would you like to back-up your existing installation before re-installing %s?

The backup will be placed on the backup partition of the destination disk (%s), overwriting any previous backups on that volume.""" % (MY_PRODUCT_BRAND, answers['installation-to-overwrite'].primary_disk),
        ['Yes', 'No', 'Back'], default=default, help='optbackup'
        )

    if button == 'back': return LEFT_BACKWARDS

    answers['backup-existing-installation'] = (button == 'yes')
    return RIGHT_FORWARDS

def dmv_more_info(context):
    global dmv_data_provider

    if not context: return True

    itemtype, item = dmv_data_provider.queryDriversOrVariant(context)
    if itemtype == "variants" or itemtype == "unknown":
        return True

    tui.update_help_line([' ', ' '])
    
    if itemtype == "drivers":
        l = []
        l.append( ("Type:", item.type) )
        l.append( ("Friendly name:", item.friendly_name) )
        l.append( ("Description:", item.description) )
        l.append( ("Info:", item.info) )
        l.append( ("Version", item.getVersion()) )
        l.append( ("Selected:", item.getSelectedText()) )
        l.append( ("Active:", item.getActiveText()) )
        for label in item.getHumanDeviceLabel():
            l.append( ("Hardware:", label) )
        snackutil.ListDialog(tui.screen, "Driver Details", l) 
    elif itemtype == "variant":
        snackutil.TableDialog(tui.screen, "Variant Details", ("Driver:", item.drvname),
                          ("Variant:", item.oemtype),
                          ("Version:", item.version),
                          ("Hardware Present:", item.getHardwarePresentText()),
                          ("Priority:", item.getPriorityText()),
                          ("Status:", item.status)) 

    tui.screen.popHelpLine()
    return True

def dmv_check_selection(answers):
    global dmv_data_provider

    hw_present_drivers = dmv_data_provider.getHardwarePresentDrivers()
    labels = list(map(lambda x:x.drvname, hw_present_drivers))

    choices = answers['selected-multiversion-drivers']
    for drvname, _ in choices:
        if drvname in labels:
            labels.remove(drvname)

    if len(labels) > 0:
        return (False, labels)
    return (True, [])

# driver multi version screen:
def dmv_screen(answers):
    global dmv_data_provider

    drivers = []
    hw_present_drivers = []
    if "selected-multiversion-drivers" not in answers:
        answers['selected-multiversion-drivers'] = []

    if not dmv_data_provider:
        dmv_data_provider = dmvutil.getDMVData()
        drivers = dmv_data_provider.getDriversData()
        dmvutil.logDriverVariants(drivers)

    # skip the ui rendering
    hw_present_drivers = dmv_data_provider.getHardwarePresentDrivers()
    if len(hw_present_drivers) == 0:
        return RIGHT_FORWARDS

    entries = []
    for d in hw_present_drivers:
        label = d.getHumanDriverLabel()
        entries.append( (label, d) )

    selected_variants = []
    for drvname, oemtype in answers['selected-multiversion-drivers']:
        logger.log("previously selected multiversion driver: %s %s" % (drvname, oemtype))
        v = dmv_data_provider.getDriverVariantByName(drvname, oemtype)
        if v:
            selected_variants.append(v)

    if len(selected_variants) == 0:
        selected_variants = dmv_data_provider.chooseDefaultDriverVariants(hw_present_drivers)
        for v in selected_variants:
            logger.log("selected-default-driver-variants: %s" % v)

    text = TextboxReflowed(54, "Select device drivers for the following hardware devices. If hardware is not present, its drivers are not shown.")
    buttons = ButtonBar(tui.screen, [('Ok', 'ok'), ('Back', 'back')])
    scroll, _ = snackutil.scrollHeight(6, len(entries))
    cbt = CheckboxTree(6, scroll)
    drv_index = 0
    for (c_text, driver) in entries:
        cbt.append(c_text, driver)
        for v in driver.getDriversVariants():
            label, variant = v
            variantSelected = 0
            if variant in selected_variants:
                variantSelected = 1
            cbt.addItem(label, (drv_index, snackArgs['append']), variant, selected = variantSelected)
        drv_index += 1

    gf = GridFormHelp(tui.screen, 'Select Driver', 'dmv:info1', 1, 6)
    gf.add(text, 0, 0, padding=(0, 0, 0, 1))
    gf.add(cbt, 0, 1, padding=(0, 0, 0, 1))
    gf.add(buttons, 0, 3, growx=1)
    gf.addHotKey('F5')
    gf.draw()

    tui.update_help_line([None, "<F5> more info"])

    loop = True
    while loop:
        rc = gf.run()
        if rc == 'F5':
            dmv_more_info(cbt.getCurrent())
        else:
            loop = False
    tui.screen.popWindow()
    tui.screen.popHelpLine()

    button = buttons.buttonPressed(rc)
    if button is None or button == 'back': return LEFT_BACKWARDS

    answers['selected-multiversion-drivers'] = []
    for variant in cbt.getSelection():
        logger.log("new selected multiversion driver: %s" % variant)
        answers['selected-multiversion-drivers'].append((variant.drvname, variant.oemtype))
    if len(answers['selected-multiversion-drivers']) == 0:
        label = "Select Driver"
        text = "No drivers selected? Go back to driver selection"
        ButtonChoiceWindow(tui.screen, label, text, ["Back"], width=48)
        return REPEAT_STEP
    else:
        ret, drvnames = dmv_check_selection(answers)
        if not ret:
            label = "Select Driver"
            message = ','.join(drvnames)
            text = "Zero selection for drivers %s? Go back to driver selection" % message
            ButtonChoiceWindow(tui.screen, label, text, ["Back"], width=48)
            return REPEAT_STEP
    return RIGHT_FORWARDS

def confirm_dmv_selection(answers):
    global dmv_data_provider

    variants = []
    choices = answers['selected-multiversion-drivers']
    for drvname, oemtype in choices:
        v = dmv_data_provider.getDriverVariantByName(drvname, oemtype)
        if v:
            variants.append(v)

    entries = []
    title = "Confirm Drivers Selection"
    text = ""
    if len(variants) == 0:
        # skip the ui rendering
        return RIGHT_FORWARDS
    else:
        got, drvname = dmv_data_provider.sameDriverMultiVariantsSelected(variants)
        if not got:
            text = "The listed drivers have been selected to take effect on %s soon after selection and upon first boot after host installation." % MY_PRODUCT_BRAND
            for item in variants:
                entries.append( (item.getHumanVariantLabel(), item) )

            scroll, height = snackutil.scrollHeight(6, len(entries))
            (button, choice) = snackutil.ListboxChoiceWindowEx(
                tui.screen, title, text,
                entries,
                ['Ok', 'Back'], 60, scroll, height, None, help='dmv:info3',
                hotkeys={})

            if button is None or button == 'back': return LEFT_BACKWARDS

            for drvname, oemtype in choices:
                logger.log("select and enable variant %s for driver %s." % (oemtype, drvname))
            failures = dmv_data_provider.applyDriverVariants(choices)
            if len(failures) == 0:
                logger.log("succeed to select and enable all driver variants.")
            else:
                for driver_name, variant_name in failures:
                    logger.log("fail to select or enable variant %s for driver %s." % (variant_name, driver_name))
                ButtonChoiceWindow(
                        tui.screen,
                        "Problem Loading Driver Variant",
                        "Setup was unable to activate driver variant.",
                        ['Ok']
                        )
            return RIGHT_FORWARDS
        else:
            title = "Error"
            text = "Only one variant of driver %s can be selected, but multiple variants of driver %s have been selected." % (drvname, drvname)
            button = snackutil.ButtonChoiceWindowEx(
                    tui.screen, title, text,
                    ['Back'], width=60, default=0, help='dmv:info4')

            if button is None or button == 'back': return LEFT_BACKWARDS
            return RIGHT_FORWARDS

def eula_screen(answers):
    eula_file = open(constants.EULA_PATH, 'r')
    eula = " ".join(eula_file.readlines())
    eula_file.close()

    while True:
        button = snackutil.ButtonChoiceWindowEx(
            tui.screen,
            "End User Agreement",
            eula,
            ['Accept EUA', 'Back'], width=60, default=1, help='eua')

        if button == 'accept eua':
            return RIGHT_FORWARDS
        elif button == 'back':
            return LEFT_BACKWARDS
        else:
            ButtonChoiceWindow(
                tui.screen,
                "End User Agreement",
                "You must select 'Accept EUA' (by highlighting it with the cursor keys, then pressing either Space or Enter) in order to install this product.",
                ['Ok'])

def confirm_erase_volume_groups(answers):
    problems = diskutil.findProblematicVGs(answers['guest-disks'])
    if len(problems) == 0:
        return SKIP_SCREEN

    if len(problems) == 1:
        logger.log("Problematic VGs: %s" % problems)
        affected = "The volume group affected is %s.  Are you sure you wish to continue?" % problems[0]
    elif len(problems) > 1:
        affected = "The volume groups affected are %s.  Are you sure you wish to continue?" % generalui.makeHumanList(problems)

    button = ButtonChoiceWindow(tui.screen,
                                "Conflicting LVM Volume Groups",
                                """Some or all of the disks you selected to install %s onto contain parts of LVM volume groups.  Proceeding with the installation will cause these volume groups to be deleted.

%s""" % (MY_PRODUCT_BRAND, affected),
                                ['Continue', 'Back'], width=60, help='erasevg')

    if button == 'back': return LEFT_BACKWARDS
    return RIGHT_FORWARDS

def use_extra_media(answers):
    rc = snackutil.ButtonChoiceWindowEx(
        tui.screen,
        "Supplemental Packs",
        "Would you like to install any Supplemental Packs?",
        ['Yes', 'No'],
        default=1, help='suppack'
        )

    answers['more-media'] = (rc != 'no')
    return RIGHT_FORWARDS

def setup_runtime_networking(answers):
    defaults = None
    try:
        if 'net-admin-interface' in answers:
            defaults = {'net-admin-interface': answers['net-admin-interface']}
            if 'runtime-iface-configuration' in answers and \
                    answers['net-admin-interface'] in answers['runtime-iface-configuration'][1]:
                defaults['net-admin-configuration'] = answers['runtime-iface-configuration'][1][answers['net-admin-interface']]
        elif 'installation-to-overwrite' in answers:
            defaults = answers['installation-to-overwrite'].readSettings()
    except:
        pass

    # Get the answers from the user
    return tui.network.requireNetworking(answers, defaults)

def disk_more_info(context):
    if not context: return True

    usage = 'unknown'
    disk = diskutil.probeDisk(context)
    if disk.root[0]:
        usage = "%s installation" % MY_PRODUCT_BRAND
    elif disk.storage[0]:
        usage = 'VM storage'

    tui.update_help_line([' ', ' '])
    snackutil.TableDialog(tui.screen, "Details", ("Disk:", diskutil.getHumanDiskName(context)),
                          ("Vendor:", diskutil.getDiskDeviceVendor(context)),
                          ("Model:", diskutil.getDiskDeviceModel(context)),
                          ("Serial:", diskutil.getDiskSerialNumber(context)),
                          ("Block size:", diskutil.bytesToHuman(diskutil.getDiskBlockSize(context))),
                          ("Size:", diskutil.getHumanDiskSize(diskutil.getDiskDeviceSize(context))),
                          ("Current usage:", usage))
    tui.screen.popHelpLine()
    return True

def sorted_disk_list(): # Smallest to largest, then alphabetical
    return sorted(diskutil.getQualifiedDiskList(), key=lambda disk: (len(disk), disk))

def confirm_disk_erase(disk):
    sr_overwrite_msg = """The selected disk, {}, contains a storage repository. The storage repository may currently be used by other hosts.

Selecting Yes will permanently erase the storage repository and any virtual machine disks it contains.

Are you sure you want to continue?"""

    return snackutil.ButtonChoiceWindowEx(tui.screen,
        "Confirm Disk Erasure",
        sr_overwrite_msg.format(diskutil.getHumanDiskLabel(disk, short=True)),
        ['Yes', 'No'], default=1)

# select drive to use as the Dom0 disk:
def select_primary_disk(answers):
    button = None
    diskEntries = sorted_disk_list()

    entries = []
    min_primary_disk_size = constants.min_primary_disk_size

    for de in diskEntries:
        (vendor, model, size) = diskutil.getExtendedDiskInfo(de)
        if diskutil.blockSizeToGBSize(size) < min_primary_disk_size:
            logger.log("disk %s is too small: %s < %s GB" %
                       (de, diskutil.blockSizeToGBSize(size), min_primary_disk_size))
            continue

        e = (diskutil.getHumanDiskLabel(de), de)
        entries.append(e)

    # we should have at least one disk
    if len(entries) == 0:
        ButtonChoiceWindow(tui.screen,
                           "No Primary Disk",
                           "No disk with sufficient space to install %s on was found." % MY_PRODUCT_BRAND,
                           ['Cancel'])
        return EXIT

    # default value:
    default = None
    if 'primary-disk' in answers:
        default = selectDefault(answers['primary-disk'], entries)

    tui.update_help_line([None, "<F5> more info"])

    scroll, height = snackutil.scrollHeight(4, len(entries))
    (button, entry) = snackutil.ListboxChoiceWindowEx(
        tui.screen,
        "Select Primary Disk",
        """Please select the disk you would like to install %s on (disks with insufficient space are not shown).

You may need to change your system settings to boot from this disk.""" % (MY_PRODUCT_BRAND),
        entries,
        ['Ok', 'Back'], 55, scroll, height, default, help='pridisk:info',
        hotkeys={'F5': disk_more_info})

    tui.screen.popHelpLine()

    if button == 'back': return LEFT_BACKWARDS

    # entry contains the 'de' part of the tuple passed in
    # determine current usage
    disk = diskutil.probeDisk(entry)
    if disk.storage[0]:
        if confirm_disk_erase(entry) == 'no':
            return REPEAT_STEP

    answers['primary-disk'] = entry

    # Warn the user if a utility partition is detected. Give them option to
    # cancel the install.
    tool = PartitionTool(answers['primary-disk'])
    if tool.partTableType != constants.PARTITION_GPT:
        if constants.GPT_SUPPORT and tool.utilityPartitions():
            val = snackutil.ButtonChoiceWindowEx(tui.screen,
                               "Preexisting utility partition detected on dos partition.",
                               "this will be overwritten do you wish to continue?",
                               ['Yes', 'No'], default=1)
            if val == 'no':
                return EXIT
            else:
                answers["preserve-first-partition"] = 'false'

    if button is None: return SKIP_SCREEN

    return RIGHT_FORWARDS

def check_sr_space(answers):
    tool = LVMTool()
    sr = tool.srPartition(answers['primary-disk'])
    assert sr

# NB. there's some stuff here that's not really consistent with XCP
# but that's ok, since it's never called on an XCP host

    root_size = constants.root_size

    button = ButtonChoiceWindow(tui.screen,
                                "Insufficient Space",
                                """The disk selected contains a storage repository which does not have enough space to also install %s on.

    Either return to the previous screen and select a different disk or cancel the installation, restart the %s and use %s to free up %dMB of space in the local storage repository.""" % (MY_PRODUCT_BRAND, BRAND_SERVER, BRAND_CONSOLE, 2 * root_size),
                                ['Back', 'Cancel'], width=60, help='insuffsr')
    if button == 'back': return LEFT_BACKWARDS

    return EXIT

def select_guest_disks(answers):
    diskEntries = sorted_disk_list()

    # CA-38329: filter out device mapper nodes (except primary disk) as these won't exist
    # at XenServer boot and therefore cannot be added as physical volumes to Local SR.
    # Also, since the DM nodes are multipathed SANs it doesn't make sense to include them
    # in the "Local" SR.
    allowed_in_local_sr = lambda dev: (dev == answers['primary-disk']) or (not isDeviceMapperNode(dev))
    diskEntries = list(filter(allowed_in_local_sr, diskEntries))

    if len(diskEntries) == 0 or constants.CC_PREPARATIONS:
        answers['guest-disks'] = []
        return SKIP_SCREEN

    # set up defaults:
    if 'guest-disks' in answers:
        currently_selected = answers['guest-disks']
    else:
        currently_selected = answers['primary-disk']

    # Make a list of entries: (text, item)
    entries = []
    for de in diskEntries:
        entries.append((diskutil.getHumanDiskLabel(de), de))

    text = TextboxReflowed(54, "Which disks do you want to use for %s storage?  \n\nOne storage repository will be created that spans the selected disks.  You can choose not to prepare any storage if you want to create an advanced configuration after installation." % BRAND_GUEST)
    buttons = ButtonBar(tui.screen, [('Ok', 'ok'), ('Back', 'back')])
    scroll, _ = snackutil.scrollHeight(3, len(entries))
    cbt = CheckboxTree(3, scroll)
    for (c_text, c_item) in entries:
        cbt.append(c_text, c_item, c_item in currently_selected)

    gf = GridFormHelp(tui.screen, 'Virtual Machine Storage', 'guestdisk:info1', 1, 4)
    gf.add(text, 0, 0, padding=(0, 0, 0, 1))
    gf.add(cbt, 0, 1, padding=(0, 0, 0, 1))
    gf.add(buttons, 0, 3, growx=1)
    gf.addHotKey('F5')

    tui.update_help_line([None, "<F5> more info"])

    loop = True
    while loop:
        rc = gf.run()
        if rc == 'F5':
            disk_more_info(cbt.getCurrent())
        else:
            loop = False
    tui.screen.popWindow()
    tui.screen.popHelpLine()

    button = buttons.buttonPressed(rc)

    if button == 'back': return LEFT_BACKWARDS

    for i in cbt.getSelection():
        # The user has already confirmed the primary disk
        if i == answers['primary-disk']:
            continue

        disk = diskutil.probeDisk(i)
        if disk.storage[0]:
            if confirm_disk_erase(i) == 'no':
                return REPEAT_STEP

    answers['guest-disks'] = cbt.getSelection()
    answers['sr-on-primary'] = answers['primary-disk'] in answers['guest-disks']

    # if the user select no disks for guest storage, check this is what
    # they wanted:
    if answers['guest-disks'] == []:
        button = ButtonChoiceWindow(
            tui.screen,
            "Warning",
            """You didn't select any disks for %s storage.  Are you sure this is what you want?

If you proceed, please refer to the user guide for details on provisioning storage after installation.""" % BRAND_GUEST,
            ['Continue', 'Back'], help='noguest'
            )
        if button == 'back': return REPEAT_STEP

    return RIGHT_FORWARDS

def get_sr_type(answers):
    guest_disks = answers['guest-disks']
    assert guest_disks is not None

    need_large_block_sr_type = any(diskutil.isLargeBlockDisk(disk)
                                   for disk in guest_disks)

    if not need_large_block_sr_type or not constants.SR_TYPE_LARGE_BLOCK:
        srtype = answers.get('sr-type', constants.SR_TYPE_LVM)
        txt = "Enable thin provisioning"
        if len(BRAND_VDI) > 0:
            txt += " (Optimized storage for %s)" % BRAND_VDI
        tb = Checkbox(txt, srtype == constants.SR_TYPE_EXT and 1 or 0)
        content = tb
        get_type = lambda: tb.selected() and constants.SR_TYPE_EXT or constants.SR_TYPE_LVM
        buttons = ButtonBar(tui.screen, [('Ok', 'ok'), ('Back', 'back')])
    else:
        content = TextboxReflowed(40,
                                  "%s storage will be configured for"
                                  " large disk block size."
                                  % BRAND_GUEST)
        get_type = lambda: constants.SR_TYPE_LARGE_BLOCK
        buttons = ButtonBar(tui.screen, [('Ok', 'ok'), ('Back', 'back')])

    gf = GridFormHelp(tui.screen, 'Virtual Machine Storage Type', 'guestdisk:info2', 1, 4)
    gf.add(content, 0, 0, padding=(0, 0, 0, 1))
    gf.add(buttons, 0, 2, growx=1)
    button = buttons.buttonPressed(gf.runOnce())

    if button == 'back': return LEFT_BACKWARDS

    if get_type:
        answers['sr-type'] = get_type()

    return RIGHT_FORWARDS

def confirm_installation(answers):
    if answers['install-type'] == constants.INSTALL_TYPE_RESTORE:
        backup = answers['backup-to-restore']
        label = "Confirm Restore"
        text = "Are you sure you want to restore your installation with the backup on %s?\n\nYour existing installation will be overwritten with the backup (though VMs will still be intact).\n\nTHIS OPERATION CANNOT BE UNDONE." % diskutil.getHumanDiskName(backup.partition)
        ok = 'Restore %s' % backup
    else:
        label = "Confirm Installation"
        text1 = "We have collected all the information required to install %s. " % MY_PRODUCT_BRAND
        if answers['install-type'] == constants.INSTALL_TYPE_FRESH:
            disks = list(map(diskutil.getHumanDiskName, answers['guest-disks']))
            if diskutil.getHumanDiskName(answers['primary-disk']) not in disks:
                disks.append(diskutil.getHumanDiskName(answers['primary-disk']))
            disks.sort()
            if len(disks) == 1:
                term = 'disk'
            else:
                term = 'disks'
            disks_used = generalui.makeHumanList(disks)
            text2 = "Please confirm you wish to proceed: all data on %s %s will be destroyed!" % (term, disks_used)
        elif answers['install-type'] == constants.INSTALL_TYPE_REINSTALL:
            if answers['primary-disk'] == answers['installation-to-overwrite'].primary_disk:
                text2 = "The installation will be performed over %s" % str(answers['installation-to-overwrite'])
            else:
                text2 = "Setup will migrate the %s installation from %s to %s" % (str(answers['installation-to-overwrite']),
                                                                                  diskutil.getHumanDiskName(answers['installation-to-overwrite'].primary_disk),
                                                                                  diskutil.getHumanDiskName(answers['primary-disk']))
            text2 += ", preserving existing %s in your storage repository." % BRAND_GUESTS
        text = text1 + "\n\n" + text2
        ok = 'Install %s' % MY_PRODUCT_BRAND

    button = snackutil.ButtonChoiceWindowEx(
        tui.screen, label, text,
        [ok, 'Back'], default=1, width=50, help='confirm'
        )

    if button is None or button == 'back': return LEFT_BACKWARDS
    return RIGHT_FORWARDS

def get_root_password(answers):
    done = False

    password_txt = "Please specify a password of at least %d characters for the root account." % (constants.MIN_PASSWD_LEN)

    if PRODUCT_BRAND:
        password_txt = "Please specify a password of at least %d characters for the root account. \n\n(This is the password used when connecting to the %s from %s.)" % (constants.MIN_PASSWD_LEN, BRAND_SERVER, BRAND_CONSOLE)

    while not done:
        (button, result) = snackutil.PasswordEntryWindow(
            tui.screen, "Set Password", password_txt,
            ['Password', 'Confirm'], buttons=['Ok', 'Back'],
            )
        if button == 'back': return LEFT_BACKWARDS

        (pw, conf) = result
        if pw == conf:
            if pw is None or len(pw) < constants.MIN_PASSWD_LEN:
                ButtonChoiceWindow(tui.screen,
                               "Password Error",
                               "The password has to be %d characters or longer." % constants.MIN_PASSWD_LEN,
                               ['Ok'], help='passwd')
            else:
                done = True
        else:
            ButtonChoiceWindow(tui.screen,
                               "Password Error",
                               "The passwords you entered did not match.  Please try again.",
                               ['Ok'])

    # if they didn't select OK we should have returned already
    assert button in ['ok', None]
    answers['root-password'] = ('plaintext', pw)
    return RIGHT_FORWARDS

def get_name_service_configuration(answers):
    # horrible hack - need a tuple due to bug in snack that means
    # we don't get an arg passed if we try to just pass False
    def hn_callback(params):
        (enabled, ) = params
        hostname.setFlags(FLAG_DISABLED, enabled)
    def ns_callback(params):
        (enabled, ) = params
        for entry in [ns1_entry, ns2_entry, ns3_entry]:
            entry.setFlags(FLAG_DISABLED, enabled)

    hide_rb = answers['net-admin-configuration'].isStatic()

    # HOSTNAME:
    hn_title = Textbox(len("Hostname Configuration"), 1, "Hostname Configuration")

    # the hostname radio group:
    if 'manual-hostname' not in answers:
        # no current value set - if we currently have a useful hostname,
        # use that, else make up a random one:
        current_hn = socket.gethostname()
        if current_hn in [None, '', '(none)', 'localhost', 'localhost.localdomain']:
            answers['manual-hostname'] = True, util.mkRandomHostname()
        else:
            answers['manual-hostname'] = True, current_hn
    use_manual_hostname, manual_hostname = answers['manual-hostname']
    if manual_hostname is None:
        manual_hostname = ""

    hn_rbgroup = RadioGroup()
    hn_dhcp_rb = hn_rbgroup.add("Automatically set via DHCP", "hn_dhcp", not use_manual_hostname)
    hn_dhcp_rb.setCallback(hn_callback, data=(False,))
    hn_manual_rb = hn_rbgroup.add("Manually specify:", "hn_manual", use_manual_hostname)
    hn_manual_rb.setCallback(hn_callback, data=(True,))

    # the hostname text box:
    hostname = Entry(hide_rb and 30 or 42, text=manual_hostname)
    hostname.setFlags(FLAG_DISABLED, use_manual_hostname)
    hostname_grid = Grid(2, 1)
    if hide_rb:
        hostname_grid.setField(Textbox(15, 1, "Hostname:"), 0, 0)
    else:
        hostname_grid.setField(Textbox(4, 1, ""), 0, 0) # spacer
    hostname_grid.setField(hostname, 1, 0)

    # NAMESERVERS:
    def nsvalue(answers, id):
        nameservers = None
        if 'manual-nameservers' in answers:
            manual, nsl = answers['manual-nameservers']
            if manual:
                nameservers = nsl
        elif 'runtime-iface-configuration' in answers:
            all_dhcp, netdict = answers['runtime-iface-configuration']
            if not all_dhcp and isinstance(netdict, dict):
                nameservers = list(netdict.values())[0].dns

        if isinstance(nameservers, list) and id < len(nameservers):
            return nameservers[id]
        return ""

    ns_title = Textbox(len("DNS Configuration"), 1, "DNS Configuration")

    use_manual_dns = nsvalue(answers, 0) != ""
    if hide_rb:
        use_manual_dns = True

    # Name server radio group
    ns_rbgroup = RadioGroup()
    ns_dhcp_rb = ns_rbgroup.add("Automatically set via DHCP", "ns_dhcp",
                                not use_manual_dns)
    ns_dhcp_rb.setCallback(ns_callback, (False,))
    ns_manual_rb = ns_rbgroup.add("Manually specify:", "ns_dhcp",
                                  use_manual_dns)
    ns_manual_rb.setCallback(ns_callback, (True,))

    # Name server text boxes
    ns1_text = Textbox(15, 1, "DNS Server 1:")
    ns1_entry = Entry(30, nsvalue(answers, 0))
    ns1_grid = Grid(2, 1)
    ns1_grid.setField(ns1_text, 0, 0)
    ns1_grid.setField(ns1_entry, 1, 0)

    ns2_text = Textbox(15, 1, "DNS Server 2:")
    ns2_entry = Entry(30, nsvalue(answers, 1))
    ns2_grid = Grid(2, 1)
    ns2_grid.setField(ns2_text, 0, 0)
    ns2_grid.setField(ns2_entry, 1, 0)

    ns3_text = Textbox(15, 1, "DNS Server 3:")
    ns3_entry = Entry(30, nsvalue(answers, 2))
    ns3_grid = Grid(2, 1)
    ns3_grid.setField(ns3_text, 0, 0)
    ns3_grid.setField(ns3_entry, 1, 0)

    if nsvalue(answers, 0) == "":
        for entry in [ns1_entry, ns2_entry, ns3_entry]:
            entry.setFlags(FLAG_DISABLED, use_manual_dns)

    done = False
    while not done:
        buttons = ButtonBar(tui.screen, [('Ok', 'ok'), ('Back', 'back')])

        # The form itself:
        i = 1
        gf = GridFormHelp(tui.screen, 'Hostname and DNS Configuration', 'dns', 1, 11)
        gf.add(hn_title, 0, 0, padding=(0, 0, 0, 0))
        if not hide_rb:
            gf.add(hn_dhcp_rb, 0, 1, anchorLeft=True)
            gf.add(hn_manual_rb, 0, 2, anchorLeft=True)
            i += 2
        gf.add(hostname_grid, 0, i, padding=(0, 0, 0, 1), anchorLeft=True)

        gf.add(ns_title, 0, i+1, padding=(0, 0, 0, 0))
        if not hide_rb:
            gf.add(ns_dhcp_rb, 0, 5, anchorLeft=True)
            gf.add(ns_manual_rb, 0, 6, anchorLeft=True)
            i += 2
        gf.add(ns1_grid, 0, i+2)
        gf.add(ns2_grid, 0, i+3)
        gf.add(ns3_grid, 0, i+4, padding=(0, 0, 0, 1))

        gf.add(buttons, 0, 10, growx=1)

        button = buttons.buttonPressed(gf.runOnce())

        if button == 'back': return LEFT_BACKWARDS

        # manual hostname?
        if hn_manual_rb.selected():
            answers['manual-hostname'] = (True, hostname.value())
        else:
            answers['manual-hostname'] = (False, None)

        # manual nameservers?
        if ns_manual_rb.selected():
            answers['manual-nameservers'] = (True, [ns1_entry.value()])
            if ns2_entry.value() != '':
                answers['manual-nameservers'][1].append(ns2_entry.value())
                if ns3_entry.value() != '':
                    answers['manual-nameservers'][1].append(ns3_entry.value())
            if 'net-admin-configuration' in answers and answers['net-admin-configuration'].isStatic():
                answers['net-admin-configuration'].dns = answers['manual-nameservers'][1]
        else:
            answers['manual-nameservers'] = (False, None)

        # validate before allowing the user to continue:
        done = True

        if hn_manual_rb.selected():
            if not netutil.valid_hostname(hostname.value(), fqdn=True):
                done = False
                ButtonChoiceWindow(tui.screen,
                                       "Name Service Configuration",
                                       "The hostname you entered was not valid.",
                                       ["Back"])
                continue
        if ns_manual_rb.selected():
            if not netutil.valid_ip_addr(ns1_entry.value()) or \
                    (ns2_entry.value() != '' and not netutil.valid_ip_addr(ns2_entry.value())) or \
                    (ns3_entry.value() != '' and not netutil.valid_ip_addr(ns3_entry.value())):
                done = False
                ButtonChoiceWindow(tui.screen,
                                   "Name Service Configuration",
                                   "Please check that you have entered at least one nameserver, and that the nameservers you specified are valid.",
                                   ["Back"])

    return RIGHT_FORWARDS

def get_timezone_region(answers):
    entries = generalui.getTimeZoneRegions()

    # default value?
    default = None
    if 'timezone-region' in answers:
        default = answers['timezone-region']

    (button, entry) = ListboxChoiceWindow(
        tui.screen,
        "Select Time Zone",
        "Please select the geographical area that your %s is in:" % BRAND_SERVER,
        entries, ['Ok', 'Back'], height=8, scroll=1,
        default=default)

    if button == 'back': return LEFT_BACKWARDS

    answers['timezone-region'] = entries[entry]
    return RIGHT_FORWARDS

def get_timezone_city(answers):
    entries = generalui.getTimeZoneCities(answers['timezone-region'])

    # default value?
    default = None
    if 'timezone-city' in answers and answers['timezone-city'] in entries:
        default = answers['timezone-city'].replace('_', ' ')

    (button, entry) = ListboxChoiceWindow(
        tui.screen,
        "Select Time Zone",
        "Please select the city or area that the managed host is in (press a letter to jump to that place in the list):",
        [x.replace('_', ' ') for x in entries],
        ['Ok', 'Back'], height=8, scroll=1, default=default, help='gettz')

    if button == 'back': return LEFT_BACKWARDS

    answers['timezone-city'] = entries[entry]
    answers['timezone'] = "%s/%s" % (answers['timezone-region'], answers['timezone-city'])
    return RIGHT_FORWARDS

def get_time_configuration_method(answers):
    ENTRY_DHCP_NTP = "Use DHCP NTP servers", "dhcp"
    ENTRY_DEFAULT_NTP = "Use default NTP servers", "default"
    ENTRY_MANUAL_NTP = "Provide NTP servers manually", "manual"
    ENTRY_NO_NTP = "No NTP (not recommended)", "none"

    entries = [ ENTRY_DEFAULT_NTP, ENTRY_MANUAL_NTP, ENTRY_NO_NTP ]
    if answers['net-admin-configuration'].mode == NetInterface.DHCP:
        entries.insert(0, ENTRY_DHCP_NTP)

    # default value?
    default = None
    if "ntp-config-method" in answers:
        default = selectDefault(answers['ntp-config-method'], entries)
    if answers['net-admin-configuration'].isStatic():
        default = ENTRY_DEFAULT_NTP

    (button, entry) = ListboxChoiceWindow(
        tui.screen,
        "System Time",
        "How should the local time be determined?",
        entries, ['Ok', 'Back'], default=default, help='timemeth')

    if button == 'back': return LEFT_BACKWARDS

    answers['ntp-config-method'] = entry
    return RIGHT_FORWARDS

def get_ntp_servers(answers):
    if answers['ntp-config-method'] != 'manual':
        return SKIP_SCREEN

    gf = GridFormHelp(tui.screen, 'NTP Configuration', 'ntpconf', 1, 4)
    text = TextboxReflowed(60, "Please specify details of the NTP servers you wish to use (e.g. pool.ntp.org)?")
    buttons = ButtonBar(tui.screen, [("Ok", "ok"), ("Back", "back")])

    def ntpvalue(answers, sn):
        if 'ntp-servers' not in answers:
            return ""
        else:
            servers = answers['ntp-servers']
            if sn < len(servers):
                return servers[sn]
            else:
                return ""

    ntp1_field = Entry(40, ntpvalue(answers, 0))
    ntp2_field = Entry(40, ntpvalue(answers, 1))
    ntp3_field = Entry(40, ntpvalue(answers, 2))

    ntp1_text = Textbox(15, 1, "NTP Server 1:")
    ntp2_text = Textbox(15, 1, "NTP Server 2:")
    ntp3_text = Textbox(15, 1, "NTP Server 3:")

    entry_grid = Grid(2, 3)
    entry_grid.setField(ntp1_text, 0, 0)
    entry_grid.setField(ntp1_field, 1, 0)
    entry_grid.setField(ntp2_text, 0, 1)
    entry_grid.setField(ntp2_field, 1, 1)
    entry_grid.setField(ntp3_text, 0, 2)
    entry_grid.setField(ntp3_field, 1, 2)

    i = 1

    gf.add(text, 0, 0, padding=(0, 0, 0, 1))
    gf.add(entry_grid, 0, i, padding=(0, 0, 0, 1))
    gf.add(buttons, 0, i+1, growx=1)

    button = buttons.buttonPressed(gf.runOnce())

    if button == 'back': return LEFT_BACKWARDS

    servers = [ntp_field.value() for ntp_field in (ntp1_field, ntp2_field, ntp3_field) if ntp_field.value() != ""]
    if len(servers) == 0:
        ButtonChoiceWindow(tui.screen,
                            "NTP Configuration",
                            "You did not specify any NTP servers",
                            ["Ok"])
        return REPEAT_STEP
    else:
        answers['ntp-servers'] = servers

    return RIGHT_FORWARDS

def set_time(answers):
    if answers['ntp-config-method'] != 'none':
        return SKIP_SCREEN

    done = False
    now = util.getLocalTime(timezone=answers['timezone'])

    # set these outside the loop so we don't overwrite them in the
    # case that the user enters a bad value.
    day = Entry(3, "%02d" % now.day, scroll=0)
    month = Entry(3, "%02d" % now.month, scroll=0)
    year = Entry(5, "%04d" % now.year, scroll=0)
    hour = Entry(3, "%02d" % now.hour, scroll=0)
    minute = Entry(3, "%02d" % now.minute, scroll=0)

    # loop until the form validates or they click back:
    while not done:
        gf = GridFormHelp(tui.screen, "Set local time", 'settime', 1, 4)

        gf.add(TextboxReflowed(50, "Please set the current (local) date and time"), 0, 0, padding=(0, 0, 1, 1))

        dategrid = Grid(7, 4)
        # TODO: switch day and month around if in appropriate timezone
        dategrid.setField(Textbox(12, 1, "Year (YYYY)"), 1, 0)
        dategrid.setField(Textbox(12, 1, "Month (MM)"), 2, 0)
        dategrid.setField(Textbox(12, 1, "Day (DD)"), 3, 0)

        dategrid.setField(Textbox(12, 1, "Hour (HH)"), 1, 2)
        dategrid.setField(Textbox(12, 1, "Min (MM)"), 2, 2)
        dategrid.setField(Textbox(12, 1, ""), 3, 2)

        dategrid.setField(Textbox(12, 1, ""), 0, 0)
        dategrid.setField(Textbox(12, 1, "Date:"), 0, 1)
        dategrid.setField(Textbox(12, 1, "Time (24h):"), 0, 3)
        dategrid.setField(Textbox(12, 1, ""), 0, 2)

        dategrid.setField(year, 1, 1, padding=(0, 0, 0, 1))
        dategrid.setField(month, 2, 1, padding=(0, 0, 0, 1))
        dategrid.setField(day, 3, 1, padding=(0, 0, 0, 1))

        dategrid.setField(hour, 1, 3)
        dategrid.setField(minute, 2, 3)

        gf.add(dategrid, 0, 1, padding=(0, 0, 1, 1))

        buttons = ButtonBar(tui.screen, [("Ok", "ok"), ("Back", "back")])
        gf.add(buttons, 0, 2, growx=1)

        button = buttons.buttonPressed(gf.runOnce())

        if button == 'back': return LEFT_BACKWARDS

        # first, check they entered something valid:
        try:
            datetime.datetime(int(year.value()),
                              int(month.value()),
                              int(day.value()),
                              int(hour.value()),
                              int(minute.value()))
        except ValueError:
            # the date was invalid - tell them why:
            done = False
            ButtonChoiceWindow(tui.screen, "Date error",
                               "The date/time you entered was not valid.  Please try again.",
                               ['Ok'])
        else:
            done = True

    # we're done:
    assert button in ['ok', None]
    answers['set-time-dialog-dismissed'] = datetime.datetime.now()
    answers['localtime'] = datetime.datetime(int(year.value()),
                                             int(month.value()),
                                             int(day.value()),
                                             int(hour.value()),
                                             int(minute.value()))
    return RIGHT_FORWARDS

def installation_complete():
    ButtonChoiceWindow(tui.screen,
                       "Installation Complete",
                       """The %s installation has completed.

Please remove any local media from the drive, and press Enter to reboot.""" % MY_PRODUCT_BRAND,
                       ['Ok'])

    return RIGHT_FORWARDS


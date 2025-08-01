# SPDX-License-Identifier: GPL-2.0-only

import os
import os.path
import stat
import subprocess
import datetime
import re
import tempfile

import repository
import generalui
import xelogging
import util
import diskutil
from disktools import *
import netutil
import shutil
import constants
import hardware
import upgrade
import init_constants
import scripts
import xcp.bootloader as bootloader
from xcp.bootloader import Grub2Format
import netinterface
import dmvutil
import tui.repo
import xcp.dom0
from xcp import logger
from xcp.version import Version

# Product version and constants:
import version
from version import *
from constants import *
from functools import reduce

MY_PRODUCT_BRAND = PRODUCT_BRAND or PLATFORM_NAME

class InvalidInstallerConfiguration(Exception):
    pass

################################################################################
# FIRST STAGE INSTALLATION:

class Task:
    """
    Represents an install step.
    'fn'   is the function to execute
    'args' is a list of value labels identifying arguments to the function,
    'returns' is a list of the labels of the return values, or a function
           that, when given the 'args' labels list, returns the list of the
           labels of the return values.
    """

    def __init__(self, fn, args, returns, args_sensitive=False,
                 progress_scale=1, pass_progress_callback=False,
                 progress_text=None):
        self.fn = fn
        self.args = args
        self.returns = returns
        self.args_sensitive = args_sensitive
        self.progress_scale = progress_scale
        self.pass_progress_callback = pass_progress_callback
        self.progress_text = progress_text

    def execute(self, answers, progress_callback=lambda x: ()):
        args = self.args(answers)
        assert type(args) == list

        if not self.args_sensitive:
            logger.log("TASK: Evaluating %s%s" % (self.fn, args))
        else:
            logger.log("TASK: Evaluating %s (sensitive data in arguments: not logging)" % self.fn)

        if self.pass_progress_callback:
            args.insert(0, progress_callback)

        rv = self.fn(*args)
        if type(rv) is not tuple:
            rv = (rv,)
        myrv = {}

        if callable(self.returns):
            ret = self.returns(*args)
        else:
            ret = self.returns

        for r in range(len(ret)):
            myrv[ret[r]] = rv[r]
        return myrv

###
# INSTALL SEQUENCES:
# convenience functions
# A: For each label in params, gives an arg function that evaluates
#    the labels when the function is called (late-binding)
# As: As above but evaluated immediately (early-binding)
# Use A when you require state values as well as the initial input values
A = lambda ans, *params: ( lambda a: [a.get(param) for param in params] )
As = lambda ans, *params: ( lambda _: [ans.get(param) for param in params] )

def getPrepSequence(ans, interactive):
    seq = [
        Task(util.getUUID, As(ans), ['installation-uuid']),
        Task(util.getUUID, As(ans), ['control-domain-uuid']),
        Task(util.randomLabelStr, As(ans), ['disk-label-suffix']),
        Task(partitionTargetDisk, A(ans, 'primary-disk', 'installation-to-overwrite', 'preserve-first-partition','sr-on-primary'),
            ['primary-partnum', 'backup-partnum', 'storage-partnum', 'boot-partnum', 'logs-partnum', 'swap-partnum']),
        ]

    if ans['ntp-config-method'] in ("dhcp", "default", "manual"):
        seq.append(Task(setTimeNTP, A(ans, 'ntp-servers', 'ntp-config-method'), []))
    elif ans['ntp-config-method'] == "none":
        seq.append(Task(setTimeManually, A(ans, 'localtime', 'set-time-dialog-dismissed', 'timezone'), []))

    if not interactive:
        seq.append(Task(verifyRepos, A(ans, 'sources', 'ui'), []))
    if ans['install-type'] == INSTALL_TYPE_FRESH:
        seq += [
            Task(removeBlockingVGs, As(ans, 'guest-disks'), []),
            Task(writeDom0DiskPartitions, A(ans, 'primary-disk', 'boot-partnum', 'primary-partnum', 'backup-partnum', 'logs-partnum', 'swap-partnum', 'storage-partnum', 'sr-at-end'),[]),
            ]
        seq.append(Task(writeGuestDiskPartitions, A(ans,'primary-disk', 'guest-disks'), []))
    elif ans['install-type'] == INSTALL_TYPE_REINSTALL:
        seq.append(Task(getUpgrader, A(ans, 'installation-to-overwrite'), ['upgrader']))
        if 'backup-existing-installation' in ans and ans['backup-existing-installation']:
            seq.append(Task(doBackup,
                            lambda a: [ a['upgrader'] ] + [ a[x] for x in a['upgrader'].doBackupArgs ],
                            lambda progress_callback, upgrader, *a: upgrader.doBackupStateChanges,
                            progress_text="Backing up existing installation...",
                            progress_scale=100,
                            pass_progress_callback=True))
        seq.append(Task(prepareTarget,
                        lambda a: [ a['upgrader'] ] + [ a[x] for x in a['upgrader'].prepTargetArgs ],
                        lambda progress_callback, upgrader, *a: upgrader.prepTargetStateChanges,
                        progress_text="Preparing target disk...",
                        progress_scale=100,
                        pass_progress_callback=True))
        seq.append(Task(prepareUpgrade,
                        lambda a: [ a['upgrader'] ] + [ a[x] for x in a['upgrader'].prepUpgradeArgs ],
                        lambda progress_callback, upgrader, *a: upgrader.prepStateChanges,
                        progress_text="Preparing for upgrade...",
                        progress_scale=100,
                        pass_progress_callback=True))
    seq += [
        Task(createDom0DiskFilesystems, A(ans, 'install-type', 'primary-disk', 'boot-partnum', 'primary-partnum', 'logs-partnum', 'disk-label-suffix', 'fs-type'), []),
        Task(mountVolumes, A(ans, 'primary-disk', 'boot-partnum', 'primary-partnum', 'logs-partnum', 'cleanup'), ['mounts', 'cleanup']),
        ]
    return seq

def getMainRepoSequence(ans, repos):
    seq = []
    seq.append(Task(repository.installFromRepos, lambda a: [repos] + [a.get('mounts')], [],
                progress_scale=100,
                pass_progress_callback=True,
                progress_text="Installing %s..." % (", ".join([repo.name() for repo in repos]))))
    for repo in repos:
        seq.append(Task(repo.record_install, A(ans, 'mounts', 'installed-repos'), ['installed-repos']))
        seq.append(Task(repo.getBranding, A(ans, 'branding'), ['branding']))
    return seq

def getRepoSequence(ans, repos):
    seq = []
    for repo in repos:
        seq.append(Task(repo.installPackages, A(ans, 'mounts'), [],
                     progress_scale=100,
                     pass_progress_callback=True,
                     progress_text="Installing %s..." % repo.name()))
        seq.append(Task(repo.record_install, A(ans, 'mounts', 'installed-repos'), ['installed-repos']))
        seq.append(Task(repo.getBranding, A(ans, 'branding'), ['branding']))
    return seq

def getFinalisationSequence(ans):
    seq = [
        Task(scripts.run_scripts, lambda a: ['packages-installed',  a['mounts']['root']], []),
        Task(writeResolvConf, A(ans, 'mounts', 'manual-hostname', 'manual-nameservers'), []),
        Task(writeMachineID, A(ans, 'mounts'), []),
        Task(writeKeyboardConfiguration, A(ans, 'mounts', 'keymap'), []),
        Task(configureNetworking, A(ans, 'mounts', 'net-admin-interface', 'net-admin-bridge', 'net-admin-configuration', 'manual-hostname', 'manual-nameservers', 'network-hardware', 'preserve-settings', 'network-backend'), []),
        Task(prepareSwapfile, A(ans, 'mounts', 'primary-disk', 'swap-partnum', 'disk-label-suffix'), []),
        Task(writeFstab, A(ans, 'mounts', 'primary-disk', 'logs-partnum', 'swap-partnum', 'disk-label-suffix', 'fs-type'), []),
        Task(enableAgent, A(ans, 'mounts', 'network-backend', 'services'), []),
        Task(configureCC, A(ans, 'mounts'), []),
        Task(writeInventory, A(ans, 'installation-uuid', 'control-domain-uuid', 'mounts', 'primary-disk',
                               'backup-partnum', 'logs-partnum', 'boot-partnum', 'swap-partnum', 'storage-partnum',
                               'guest-disks', 'net-admin-bridge',
                               'branding', 'net-admin-configuration', 'host-config', 'install-type'), []),
        Task(writeXencommons, A(ans, 'control-domain-uuid', 'mounts'), []),
        Task(configureISCSI, A(ans, 'mounts', 'primary-disk'), []),
        Task(mkinitrd, A(ans, 'mounts', 'primary-disk', 'primary-partnum'), []),
        Task(prepFallback, A(ans, 'mounts', 'primary-disk', 'primary-partnum'), []),
        Task(installBootLoader, A(ans, 'mounts', 'primary-disk',
                                  'boot-partnum', 'primary-partnum', 'branding',
                                  'disk-label-suffix', 'bootloader-location', 'write-boot-entry', 'install-type',
                                  'serial-console', 'boot-serial', 'host-config'), []),
        Task(touchSshAuthorizedKeys, A(ans, 'mounts'), []),
        Task(setRootPassword, A(ans, 'mounts', 'root-password'), [], args_sensitive=True),
        Task(setTimeZone, A(ans, 'mounts', 'timezone'), []),
        Task(writei18n, A(ans, 'mounts'), []),
        Task(configureMCELog, A(ans, 'mounts'), []),
        Task(writeDMVSelections, A(ans, 'mounts', 'selected-multiversion-drivers'), []),
        ]

    # on fresh installs, prepare the storage repository as required:
    if ans['install-type'] == INSTALL_TYPE_FRESH:
        seq += [
            Task(prepareStorageRepositories, A(ans, 'mounts', 'primary-disk', 'storage-partnum', 'guest-disks', 'sr-type'), []),
            Task(configureSRMultipathing, A(ans, 'mounts', 'primary-disk'), []),
            ]

    seq.append(Task(setDHCPNTP, A(ans, "mounts", "ntp-config-method"), []))
    if ans['ntp-config-method'] != "none":
        seq.append(Task(configureNTP, A(ans, 'mounts', 'ntp-config-method', 'ntp-servers'), []))
    # complete upgrade if appropriate:
    if ans['install-type'] == constants.INSTALL_TYPE_REINSTALL:
        seq.append( Task(completeUpgrade, lambda a: [ a['upgrader'] ] + [ a[x] for x in a['upgrader'].completeUpgradeArgs ], []) )

    # run the users's scripts
    seq.append( Task(scripts.run_scripts, lambda a: ['filesystem-populated',  a['mounts']['root']], []) )

    seq.append(Task(umountVolumes, A(ans, 'mounts', 'cleanup'), ['cleanup']))
    seq.append(Task(writeLog, A(ans, 'primary-disk', 'primary-partnum', 'logs-partnum'), []))

    return seq

def prettyLogAnswers(answers):
    for a in answers:
        if a == 'root-password':
            val = (answers[a][0], '< not printed >')
        elif a == 'pool-token':
            val = '< not printed >'
        else:
            val = answers[a]
        logger.log("%s := %s %s" % (a, val, type(val)))

def executeSequence(sequence, seq_name, answers, ui, cleanup):
    answers['cleanup'] = []
    answers['ui'] = ui

    progress_total = reduce(lambda x, y: x + y,
                            [task.progress_scale for task in sequence])

    pd = None
    if ui:
        pd = ui.progress.initProgressDialog(
            "Installing %s" % MY_PRODUCT_BRAND,
            seq_name, progress_total
            )
    logger.log("DISPATCH: NEW PHASE: %s" % seq_name)

    def doCleanup(actions):
        for tag, f, a in actions:
            try:
                f(*a)
            except:
                logger.log("FAILED to perform cleanup action %s" % tag)

    def progressCallback(x):
        if ui:
            ui.progress.displayProgressDialog(current + x, pd)

    try:
        current = 0
        for item in sequence:
            if pd:
                if item.progress_text:
                    text = item.progress_text
                else:
                    text = seq_name

                ui.progress.displayProgressDialog(current, pd, updated_text=text)
            updated_state = item.execute(answers, progressCallback)
            if len(updated_state) > 0:
                logger.log(
                    "DISPATCH: Updated state: %s" %
                    "; ".join(["%s -> %s" % (k, v) for k, v in updated_state.items()])
                    )
                for state_item in updated_state:
                    answers[state_item] = updated_state[state_item]

            current = current + item.progress_scale
    except:
        doCleanup(answers['cleanup'])
        raise
    else:
        if ui and pd:
            ui.progress.clearModelessDialog()

        if cleanup:
            doCleanup(answers['cleanup'])
            del answers['cleanup']

def performInstallation(answers, ui_package, interactive):
    logger.log("INPUT ANSWERS DICTIONARY:")
    prettyLogAnswers(answers)
    logger.log("SCRIPTS DICTIONARY:")
    prettyLogAnswers(scripts.script_dict)

    dom0_mem = xcp.dom0.default_memory_for_version(
                    hardware.getHostTotalMemoryKB(),
                    Version.from_string(version.PLATFORM_VERSION)) // 1024
    dom0_vcpus = xcp.dom0.default_vcpus(hardware.getHostTotalCPUs(), dom0_mem)
    default_host_config = { 'dom0-mem': dom0_mem,
                            'dom0-vcpus': dom0_vcpus,
                            'xen-cpuid-masks': [] }
    defaults = { 'branding': {}, 'host-config': {}, 'write-boot-entry': True }

    # update the settings:
    if answers['preserve-settings'] == True:
        defaults.update({ 'guest-disks': [] })

        logger.log("Updating answers dictionary based on existing installation")
        try:
            answers.update(answers['installation-to-overwrite'].readSettings())

            # Use the new default amount of RAM as long as it doesn't result in
            # a decrease from the previous installation. Update the number of
            # dom0 vCPUs since it depends on the amount of RAM assigned.
            if 'dom0-mem' in answers['host-config']:
                answers['host-config']['dom0-mem'] = max(answers['host-config']['dom0-mem'],
                                                         default_host_config['dom0-mem'])
                default_host_config['dom0-vcpus'] = xcp.dom0.default_vcpus(hardware.getHostTotalCPUs(),
                                                                           answers['host-config']['dom0-mem'])
        except Exception as e:
            logger.logException(e)
            raise RuntimeError("Failed to get existing installation settings")

        prettyLogAnswers(answers)
    else:
        defaults.update({ 'master': None,
                          'sr-type': constants.SR_TYPE_LVM,
                          'bootloader-location': constants.BOOT_LOCATION_MBR,
                          'sr-at-end': True,
                          'sr-on-primary': True})

        logger.log("Updating answers dictionary based on defaults")

    for k, v in defaults.items():
        if k not in answers:
            answers[k] = v
    for k, v in default_host_config.items():
        if k not in answers['host-config']:
            answers['host-config'][k] = v
    logger.log("UPDATED ANSWERS DICTIONARY:")
    prettyLogAnswers(answers)

    # Slight hack: we need to write the bridge name to xensource-inventory
    # further down; compute it here based on the admin interface name if we
    # haven't already recorded it as part of reading settings from an upgrade:
    if answers['install-type'] == INSTALL_TYPE_FRESH:
        answers['net-admin-bridge'] = ''
    elif 'net-admin-bridge' not in answers:
        assert answers['net-admin-interface'].startswith("eth")
        answers['net-admin-bridge'] = "xenbr%s" % answers['net-admin-interface'][3:]

    # perform installation:
    prep_seq = getPrepSequence(answers, interactive)
    answers_pristine = answers.copy()
    executeSequence(prep_seq, "Preparing for installation...", answers, ui_package, False)

    # install from main repositories:
    def handleMainRepos(main_repositories, ans):
        repo_seq = getMainRepoSequence(ans, main_repositories)
        executeSequence(repo_seq, "Reading package information...", ans, ui_package, False)

    def handleRepos(repos, ans):
        repo_seq = getRepoSequence(ans, repos)
        executeSequence(repo_seq, "Reading package information...", ans, ui_package, False)

    answers['installed-repos'] = {}

    # A list needs to be used rather than a set since the order of updates is
    # important.  However, since the same repository might exist in multiple
    # locations or the same location might be listed multiple times, care is
    # needed to ensure that there are no duplicates.
    main_repositories = []
    update_repositories = []

    def add_repos(main_repositories, update_repositories, repos):
        """Add repositories to the appropriate list, ensuring no duplicates,
        that the main repository is at the beginning, and that the order of the
        rest is maintained."""

        for repo in repos:
            if isinstance(repo, repository.UpdateYumRepository):
                repo_list = update_repositories
            else:
                repo_list = main_repositories

            if repo not in repo_list:
                if repo.identifier() == MAIN_REPOSITORY_NAME:
                    repo_list.insert(0, repo)
                else:
                    repo_list.append(repo)

    # A list of sources coming from the answerfile
    if 'sources' in answers_pristine:
        for i in answers_pristine['sources']:
            repos = repository.repositoriesFromDefinition(i['media'], i['address'])
            add_repos(main_repositories, update_repositories, repos)

    # A single source coming from an interactive install
    if 'source-media' in answers_pristine and 'source-address' in answers_pristine:
        repos = repository.repositoriesFromDefinition(answers_pristine['source-media'], answers_pristine['source-address'])
        add_repos(main_repositories, update_repositories, repos)

    for media, address in answers_pristine['extra-repos']:
        repos = repository.repositoriesFromDefinition(media, address)
        add_repos(main_repositories, update_repositories, repos)

    if not main_repositories or main_repositories[0].identifier() != MAIN_REPOSITORY_NAME:
        raise RuntimeError("No main repository found")

    handleMainRepos(main_repositories, answers)
    if update_repositories:
        handleRepos(update_repositories, answers)

    # Find repositories that we installed from removable media
    # and eject the media.
    for r in main_repositories + update_repositories:
        if r.accessor().canEject():
            r.accessor().eject()

    if interactive and constants.HAS_SUPPLEMENTAL_PACKS:
        # Add supp packs in a loop
        while True:
            media_ans = dict(answers_pristine)
            del media_ans['source-media']
            del media_ans['source-address']
            media_ans = ui_package.installer.more_media_sequence(media_ans)
            if 'more-media' not in media_ans or not media_ans['more-media']:
                break

            repos = repository.repositoriesFromDefinition(media_ans['source-media'], media_ans['source-address'])
            repos = set([repo for repo in repos if str(repo) not in answers['installed-repos']])
            if not repos:
                continue
            handleRepos(repos, answers)

            for r in repos:
                if r.accessor().canEject():
                    r.accessor().eject()

    # complete the installation:
    fin_seq = getFinalisationSequence(answers)
    executeSequence(fin_seq, "Completing installation...", answers, ui_package, True)

def configureMCELog(mounts):
    """Disable mcelog on unsupported processors."""

    is_amd = False
    model = 0

    with open('/proc/cpuinfo', 'r') as f:
        for line in f:
            line = line.strip()
            if re.match('vendor_id\s*:\s*AuthenticAMD$', line):
                is_amd = True
                continue
            m = re.match('cpu family\s*:\s*(\d+)$', line)
            if m:
                model = int(m.group(1))

    if is_amd and model >= 16:
        util.runCmd2(['chroot', mounts['root'], 'systemctl', 'disable', 'mcelog'])

def rewriteNTPConf(root, ntp_servers):
    ntpsconf = open("%s/etc/chrony.conf" % root, 'r')
    lines = ntpsconf.readlines()
    ntpsconf.close()

    lines = [x for x in lines if not x.startswith('server ')]

    ntpsconf = open("%s/etc/chrony.conf" % root, 'w')
    for line in lines:
        ntpsconf.write(line)

    if ntp_servers:
        for server in ntp_servers:
            ntpsconf.write("server %s iburst\n" % server)
    ntpsconf.close()

def setTimeNTP(ntp_servers, ntp_config_method):
    if ntp_config_method in ("dhcp", "manual"):
        rewriteNTPConf('', ntp_servers)

    # This might fail or stall if the network is not set up correctly so set a
    # time limit and don't expect it to succeed.
    if util.runCmd2(['timeout', '15', 'chronyd', '-q']) == 0:
        assert util.runCmd2(['hwclock', '--utc', '--systohc']) == 0

def setTimeManually(localtime, set_time_dialog_dismissed, timezone):
    newtime = localtime + (datetime.datetime.now() - set_time_dialog_dismissed)
    timestr = "%04d-%02d-%02d %02d:%02d:00" % \
              (newtime.year, newtime.month, newtime.day,
               newtime.hour, newtime.minute)

    util.setLocalTime(timestr, timezone=timezone)
    assert util.runCmd2(['hwclock', '--utc', '--systohc']) == 0

def setDHCPNTP(mounts, ntp_config_method):
    script = os.path.join(mounts['root'], "etc/dhcp/dhclient.d/chrony.sh")
    oldPermissions = os.stat(script).st_mode

    if ntp_config_method == "dhcp":
        newPermissions = oldPermissions | (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)  # Add execute permission
    else:
        newPermissions = oldPermissions & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)  # Remove execute permission

    os.chmod(script, newPermissions)

def configureNTP(mounts, ntp_config_method, ntp_servers):
    # If NTP servers were specified, update the NTP config file:
    if ntp_config_method in ("dhcp", "manual"):
        rewriteNTPConf(mounts['root'], ntp_servers)

    # now turn on the ntp service:
    util.runCmd2(['chroot', mounts['root'], 'systemctl', 'enable', 'chronyd'])
    util.runCmd2(['chroot', mounts['root'], 'systemctl', 'enable', 'chrony-wait'])

# This is attempting to understand the desired layout of the future partitioning
# based on options passed and status of disk (like partition to retain).
# This should be used for upgrade or install, not for restore.
# Returns 'primary-partnum', 'backup-partnum', 'storage-partnum', 'boot-partnum', 'logs-partnum', 'swap-partnum'
def partitionTargetDisk(disk, existing, preserve_first_partition, create_sr_part):
    primary_part = 1
    if existing:
        # upgrade, use existing partitioning scheme
        tool = PartitionTool(existing.primary_disk)

        primary_part = tool.partitionNumber(existing.root_device)

        # Determine target install's boot partition number
        if existing.boot_device:
            boot_partnum = tool.partitionNumber(existing.boot_device)
            boot_part = tool.getPartition(boot_partnum)
            if 'id' not in boot_part or boot_part['id'] != GPTPartitionTool.ID_EFI_BOOT:
                raise RuntimeError("Boot partition is not set up for UEFI mode or missing EFI partition ID.")
        else:
            boot_partnum = primary_part + 3

        logger.log("Upgrading")

        # Return install mode and numbers of primary, backup, SR, boot, log and swap partitions
        storage_partition = tool.getPartition(primary_part+2)
        if storage_partition:
            return (primary_part, primary_part+1, primary_part+2, boot_partnum, primary_part+4, primary_part+5)
        else:
            return (primary_part, primary_part+1, 0, boot_partnum, primary_part+4, primary_part+5)

    tool = PartitionTool(disk)

    # Cannot preserve partition for legacy DOS partition table.
    if tool.partTableType == constants.PARTITION_DOS :
        if preserve_first_partition == 'true' or (
                preserve_first_partition == constants.PRESERVE_IF_UTILITY and tool.utilityPartitions()):
            raise RuntimeError("Preserving initial partition on DOS unsupported")

    # Preserve any utility partitions unless user told us to zap 'em
    primary_part = 1
    if preserve_first_partition == 'true':
        if tool.getPartition(1) is None:  # If no first partition
            raise RuntimeError("No first partition to preserve")
        primary_part += 1
    elif preserve_first_partition == constants.PRESERVE_IF_UTILITY:
        utilparts = tool.utilityPartitions()
        primary_part += max(utilparts+[0])
        if primary_part > 2:
            raise RuntimeError("Installer only supports a single Utility Partition at partition 1, but found Utility Partitions at %s" % str(utilparts))

    sr_part = -1
    if create_sr_part:
        sr_part = primary_part+2

    boot_part = max(primary_part + 1, sr_part) + 1

    logger.log("Fresh install")

    return (primary_part, primary_part + 1, sr_part, boot_part, primary_part + 4, primary_part + 5)

def removeBlockingVGs(disks):
    for vg in diskutil.findProblematicVGs(disks):
        util.runCmd2(['vgreduce', '--removemissing', vg])
        util.runCmd2(['lvremove', vg])
        util.runCmd2(['vgremove', vg])

###
# Functions to write partition tables to disk
def writeDom0DiskPartitions(disk, boot_partnum, primary_partnum, backup_partnum, logs_partnum, swap_partnum, storage_partnum, sr_at_end):

    # we really don't want to screw this up...
    assert type(disk) == str
    assert disk[:5] == '/dev/'

    if not os.path.exists(disk):
        raise RuntimeError("The disk %s could not be found." % disk)

    # If new partition layout requested: exit if disk is not big enough, otherwise implement it
    elif diskutil.blockSizeToGBSize(diskutil.getDiskDeviceSize(disk)) < constants.min_primary_disk_size:
        raise RuntimeError("The disk %s is smaller than %dGB." % (disk, constants.min_primary_disk_size))

    tool = PartitionTool(disk, constants.PARTITION_GPT)
    for num, part in tool.items():
        if num >= primary_partnum:
            tool.deletePartition(num)

    order = primary_partnum


    # Create the new partition layout (5,2,1,4,6,3)
    # Normal layout       
    # 1 - dom0 partition  
    # 2 - backup partition
    # 3 - LVM partition   
    # 4 - Boot partition  
    # 5 - logs partition  
    # 6 - swap partition  
    #                     

    # Create logs partition
    # Start the first partition at 1 MiB if there are no other partitions.
    # Otherwise start the partition following the utility partition.
    if order == 1:
        tool.createPartition(tool.ID_LINUX, sizeBytes=logs_size * 2**20, startBytes=2**20, number=logs_partnum, order=order, label=logspart_label)
    else:
        tool.createPartition(tool.ID_LINUX, sizeBytes=logs_size * 2**20, number=logs_partnum, order=order, label=logspart_label)
    order += 1

    # Create backup partition
    if backup_partnum > 0:
        tool.createPartition(tool.ID_LINUX, sizeBytes=backup_size * 2**20, number=backup_partnum, order=order, label=backuppart_label)
        order += 1

    # Create dom0 partition
    tool.createPartition(tool.ID_LINUX, sizeBytes=constants.root_size * 2**20, number=primary_partnum, order=order, label=rootpart_label)
    order += 1

    # Create Boot partition
    tool.createPartition(tool.ID_EFI_BOOT, sizeBytes=boot_size * 2**20, number=boot_partnum, order=order, label=bootpart_label)
    order += 1

    # Create swap partition
    tool.createPartition(tool.ID_LINUX_SWAP, sizeBytes=swap_size * 2**20, number=swap_partnum, order=order, label=swappart_label)
    order += 1

    # Create LVM partition
    if storage_partnum > 0:
        tool.createPartition(tool.ID_LINUX_LVM, number=storage_partnum, order=order, label=storagepart_label)
        order += 1

    if not sr_at_end:
        # For upgrade testing, out-of-order partition layout
        new_parts = {}

        new_parts[primary_partnum] = {'start': tool.partitions[primary_partnum]['start'] + tool.partitions[storage_partnum]['size'],
                                      'size': tool.partitions[primary_partnum]['size'],
                                      'id': tool.partitions[primary_partnum]['id'],
                                      'active': tool.partitions[primary_partnum]['active'],
                                      'partlabel': tool.partitions[primary_partnum]['partlabel']}
        if backup_partnum > 0:
            new_parts[backup_partnum] = {'start': new_parts[primary_partnum]['start'] + new_parts[primary_partnum]['size'],
                                         'size': tool.partitions[backup_partnum]['size'],
                                         'id': tool.partitions[backup_partnum]['id'],
                                         'active': tool.partitions[backup_partnum]['active'],
                                         'partlabel': tool.partitions[backup_partnum]['partlabel']}

        new_parts[storage_partnum] = {'start': tool.partitions[primary_partnum]['start'],
                                      'size': tool.partitions[storage_partnum]['size'],
                                      'id': tool.partitions[storage_partnum]['id'],
                                      'active': tool.partitions[storage_partnum]['active'],
                                      'partlabel': tool.partitions[storage_partnum]['partlabel']}

        for part in (primary_partnum, backup_partnum, storage_partnum):
            if part > 0:
                tool.deletePartition(part)
                tool.createPartition(new_parts[part]['id'], new_parts[part]['size'] * tool.sectorSize, part,
                                     new_parts[part]['start'] * tool.sectorSize, new_parts[part]['active'],
                                     new_parts[part]['partlabel'])

    tool.commit(log=True)

def writeGuestDiskPartitions(primary_disk, guest_disks):
    # At the moment this code uses the same partition table type for Guest Disks as it
    # does for the root disk.  But we could choose to always use 'GPT' for guest disks.
    # TODO: Decide!
    for gd in guest_disks:
        if gd != primary_disk:
            # we really don't want to screw this up...
            assert type(gd) == str
            assert gd[:5] == '/dev/'

            tool = PartitionTool(gd, constants.PARTITION_GPT)
            tool.deletePartitions(list(tool.partitions.keys()))
            tool.commit(log=True)


def setActiveDiskPartition(disk, boot_partnum, primary_partnum):
    tool = PartitionTool(disk, constants.PARTITION_GPT)
    tool.commitActivePartitiontoDisk(boot_partnum)

def getSRPhysDevs(primary_disk, storage_partnum, guest_disks):
    def sr_partition(disk):
        if disk == primary_disk:
            return partitionDevice(disk, storage_partnum)
        else:
            return disk

    return [sr_partition(disk) for disk in guest_disks]

def prepareStorageRepositories(mounts, primary_disk, storage_partnum, guest_disks, sr_type):

    if len(guest_disks) == 0 or constants.CC_PREPARATIONS and sr_type != constants.SR_TYPE_EXT:
        logger.log("No storage repository requested.")
        return None

    logger.log("Arranging for storage repositories to be created at first boot...")

    partitions = getSRPhysDevs(primary_disk, storage_partnum, guest_disks)

    # write a config file for the prepare-storage firstboot script:

    links = [diskutil.idFromPartition(x) or x for x in partitions]
    fd = open(os.path.join(mounts['root'], constants.FIRSTBOOT_DATA_DIR, 'default-storage.conf'), 'w')
    print("XSPARTITIONS='%s'" % str.join(" ", links), file=fd)
    print("XSTYPE='%s'" % sr_type, file=fd)
    # Legacy names
    print("PARTITIONS='%s'" % str.join(" ", links), file=fd)
    print("TYPE='%s'" % sr_type, file=fd)
    fd.close()

def make_free_space(mount, required):
    """Make required bytes of free space available on mount by removing files,
    oldest first."""

    def getinfo(dirpath, name):
        path = os.path.join(dirpath, name)
        return os.stat(path).st_mtime, path

    def free_space(path):
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize

    if free_space(mount) >= required:
        return

    files = []
    dirs = []

    for dirpath, dirnames, filenames in os.walk(mount):
        for i in dirnames:
            dirs.append(getinfo(dirpath, i))
        for i in filenames:
            files.append(getinfo(dirpath, i))

    files.sort()
    dirs.sort()

    for _, path in files:
        os.unlink(path)
        logger.log('Removed %s' % path)
        if free_space(mount) >= required:
            return

    for _, path in dirs:
        shutil.rmtree(path, ignore_errors=True)
        logger.log('Removed %s' % path)
        if free_space(mount) >= required:
            return

    raise RuntimeError("Failed to make enough space available on %s (%d, %d)" % (mount, required, free_space(mount)))

###
# Create dom0 disk file-systems:

def createDom0DiskFilesystems(install_type, disk, boot_partnum, primary_partnum, logs_partnum, disk_label_suffix, fs_type):
    partition = partitionDevice(disk, boot_partnum)
    try:
        util.mkfs(bootfs_type, partition,
                    ["-n", bootfs_label%disk_label_suffix.upper()])
    except Exception as e:
        raise RuntimeError("Failed to create boot filesystem: %s" % e)

    partition = partitionDevice(disk, primary_partnum)
    try:
        util.mkfs(fs_type, partition,
                  ["-L", rootfs_label%disk_label_suffix])
    except Exception as e:
        raise RuntimeError("Failed to create root filesystem: %s" % e)

    tool = PartitionTool(disk)
    logs_partition = tool.getPartition(logs_partnum)
    if logs_partition:
        run_mkfs = True
        change_logs_fs_type = diskutil.fs_type_from_device(partitionDevice(disk, logs_partnum)) != fs_type

        # If the log partition already exists and is formatted correctly,
        # relabel it. Otherwise create the filesystem.
        partition = partitionDevice(disk, logs_partnum)
        label = None
        try:
            label = diskutil.readExtPartitionLabel(partition)
        except Exception as e:
            # Ignore the exception as it just means the partition needs to be
            # formatted.
            pass
        if install_type != INSTALL_TYPE_FRESH and label and label.startswith(logsfs_label_prefix) and not change_logs_fs_type:
            # If a filesystem which has not been unmounted cleanly is
            # relabelled, it will revert to the original label once it is
            # mounted. To prevent this, fsck the filesystem before relabelling.
            # If any unfixable errors occur or relabelling fails, just recreate
            # the filesystem instead, rather than fail the installation.
            if util.runCmd2(['e2fsck', '-y', partition]) in (0, 1):
                if util.runCmd2(['e2label', partition, constants.logsfs_label % disk_label_suffix]) == 0:
                    run_mkfs = False

        if run_mkfs:
            try:
                util.mkfs(fs_type, partition,
                          ["-L", logsfs_label % disk_label_suffix])
            except Exception as e:
                raise RuntimeError("Failed to create logs filesystem: %s" % e)
        else:
            # Ensure enough free space is available
            mount = util.TempMount(partition, 'logs-')
            try:
                make_free_space(mount.mount_point, constants.logs_free_space * 1024 * 1024)
            finally:
                mount.unmount()

#pylint: disable=consider-using-f-string
def _generateBFS(mounts, primary_disk): #pylint: disable=invalid-name
    rv, wwid, err = util.runCmd2(["chroot", mounts["root"], "/usr/lib/udev/scsi_id",
                               "-g", primary_disk], with_stdout=True, with_stderr=True)
    if rv != 0:
        raise RuntimeError("Failed to whitelist %s with error: %s" % (primary_disk, err) )

    # Remove ending line breaker
    wwid = wwid.strip()

    util.runCmd2(["chroot", mounts["root"], "/usr/sbin/multipath", "-a", wwid])

def __mkinitrd(mounts, primary_disk, partition, kernel_version):
    if isDeviceMapperNode(partition):
        # Generate a valid multipath configuration
        _generateBFS(mounts, primary_disk)

    # Run dracut inside dom0 chroot
    output_file = os.path.join("/boot", "initrd-%s.img" % kernel_version)

    # default to only including host specific kernel modules in initrd
    # disable multipath on root partition
    try:
        if not isDeviceMapperNode(partition):
            f = open(os.path.join(mounts['root'], 'etc/dracut.conf.d/xs_disable_multipath.conf'), 'w')
            f.write('omit_dracutmodules+=" multipath "\n')
            f.close()
    except:
        pass

    cmd = ['dracut', '--verbose', '-f', output_file, kernel_version]

    if util.runCmd2(['chroot', mounts['root']] + cmd) != 0:
        raise RuntimeError("Failed to create initrd for %s.  This is often due to using an installer that is not the same version of %s as your installation source." % (kernel_version, MY_PRODUCT_BRAND))

    # CA-412051: debug logging, will revert in future
    util.runCmd2(['chroot', mounts['root'], 'ldd', '/usr/sbin/init'])
    util.runCmd2(['chroot', mounts['root'], 'rpm', '-ql', 'systemd'])
    util.runCmd2(['chroot', mounts['root'], 'lsinitrd', output_file])

def getXenVersion(rootfs_mount):
    """ Return the xen version by interogating the package version in the chroot """
    xen_version = ['rpm', '--root', rootfs_mount, '-q', '--qf', '%{version}', 'xen-hypervisor']
    rc, out = util.runCmd2(xen_version, with_stdout=True)
    if rc != 0:
        return None
    return out

def getKernelVersion(rootfs_mount):
    """ Returns the kernel release (uname -r) of the installed kernel """
    kernel_version = ['rpm', '--root', rootfs_mount, '-q', '--provides', 'kernel']
    rc, out = util.runCmd2(kernel_version, with_stdout=True)
    if rc != 0:
        return None

    try:
        uname_provides = [x for x in out.split('\n') if x.startswith('kernel-uname-r')]
        return uname_provides[0].split('=')[1].strip()
    except:
        pass
    return None

def kernelShortVersion(version):
    """ Return the short kernel version string (i.e., just major.minor). """
    parts = version.split(".")
    return parts[0] + "." + parts[1]

def configureSRMultipathing(mounts, primary_disk):
    # Only called on fresh installs:
    # Configure multipathed SRs iff root disk is multipathed
    fd = open(os.path.join(mounts['root'], constants.FIRSTBOOT_DATA_DIR, 'sr-multipathing.conf'),'w')
    if isDeviceMapperNode(primary_disk):
        fd.write("MULTIPATHING_ENABLED='True'\n")
    else:
        fd.write("MULTIPATHING_ENABLED='False'\n")
    fd.close()

def adjustISCSITimeoutForFile(path):
    iscsiconf = open(path, 'r')
    lines = iscsiconf.readlines()
    iscsiconf.close()

    timeout_key = "node.session.timeo.replacement_timeout"
    wrote_key = False
    iscsiconf = open(path, 'w')
    for line in lines:
        if line.startswith(timeout_key):
            iscsiconf.write("%s = %d\n" % (timeout_key, MPATH_ISCSI_TIMEOUT))
            wrote_key = True
        else:
            iscsiconf.write(line)
    if not wrote_key:
        iscsiconf.write("%s = %d\n" % (timeout_key, MPATH_ISCSI_TIMEOUT))

    iscsiconf.close()

def configureISCSI(mounts, primary_disk):
    if not diskutil.is_iscsi(primary_disk):
        return

    iname = diskutil.get_initiator_name()

    with open(os.path.join(mounts['root'], 'etc/iscsi/initiatorname.iscsi'), 'w') as f:
        f.write('InitiatorName=%s\n' % (iname,))

    # Create IQN file for XAPI
    with open(os.path.join(mounts['root'], 'etc/firstboot.d/data/iqn.conf'), 'w') as f:
        f.write("IQN='%s'" % iname)

    if util.runCmd2(['chroot', mounts['root'],
                     'systemctl', 'enable', 'iscsid']):
        raise RuntimeError("Failed to enable iscsid")
    if util.runCmd2(['chroot', mounts['root'],
                     'systemctl', 'enable', 'iscsi']):
        raise RuntimeError("Failed to enable iscsi")

    diskutil.write_iscsi_records(mounts, primary_disk)

    # Reduce the timeout when using multipath
    if isDeviceMapperNode(primary_disk):
        adjustISCSITimeoutForFile("%s/etc/iscsi/iscsid.conf" % mounts['root'])

def mkinitrd(mounts, primary_disk, primary_partnum):
    xen_version = getXenVersion(mounts['root'])
    if xen_version is None:
        raise RuntimeError("Unable to determine Xen version.")
    xen_kernel_version = getKernelVersion(mounts['root'])
    if not xen_kernel_version:
        raise RuntimeError("Unable to determine kernel version.")
    partition = partitionDevice(primary_disk, primary_partnum)


    __mkinitrd(mounts, primary_disk, partition, xen_kernel_version)

def prepFallback(mounts, primary_disk, primary_partnum):
    kernel_version =  getKernelVersion(mounts['root'])

    # Copy /boot/xen-xxxx.efi to /boot/xen-fallback.efi
    xen_efi = os.path.realpath(mounts['root'] + "/boot/xen.efi")
    src = os.path.join(mounts['root'], "boot", os.path.basename(xen_efi))
    dst = os.path.join(mounts['root'], 'boot/xen-fallback.efi')
    shutil.copyfile(src, dst)

    # Copy /boot/vmlinuz-yyyy to /boot/vmlinuz-fallback
    src = os.path.join(mounts['root'], 'boot/vmlinuz-%s' % kernel_version)
    dst = os.path.join(mounts['root'], 'boot/vmlinuz-fallback')
    shutil.copyfile(src, dst)

    # Extra modules to include in the fallback initrd.  Include all
    # currently loaded modules so the network module is picked up.
    modules = []
    proc_modules = open('/proc/modules', 'r')
    for line in proc_modules:
        modules.append(line.split(' ')[0])
    proc_modules.close()

    # Generate /boot/initrd-fallback.img.
    cmd = ['dracut', '--verbose', '--add-drivers', ' '.join(modules), '--no-hostonly']
    cmd += ['/boot/initrd-fallback.img', kernel_version]

    if util.runCmd2(['chroot', mounts['root']] + cmd):
        raise RuntimeError("Failed to generate fallback initrd")


def buildBootLoaderMenu(mounts, xen_version, xen_kernel_version, boot_config, serial, boot_serial, host_config, primary_disk, disk_label_suffix):
    short_version = kernelShortVersion(xen_kernel_version)
    common_xen_params = "dom0_mem=%dM,max:%dM" % ((host_config['dom0-mem'],) * 2)
    common_xen_unsafe_params = "watchdog dom0_max_vcpus=1-%d" % host_config['dom0-vcpus']
    safe_xen_params = ("nosmp noreboot noirqbalance no-mce no-bootscrub "
                       "no-numa no-hap no-mmcfg max_cstate=0 "
                       "nmi=ignore allow_unsafe")
    xen_mem_params = "crashkernel=256M,below=4G"

    # CA-103933 - AMD PCI-X Hypertransport Tunnel IOAPIC errata
    rc, out = util.runCmd2(['lspci', '-n'], with_stdout=True)
    if rc == 0 and ('1022:7451' in out or '1022:7459' in out):
        common_xen_params += " ioapic_ack=old"

    if "sched-gran" in host_config:
        common_xen_params += " %s" % host_config["sched-gran"]

    common_kernel_params = "root=LABEL=%s ro nolvm hpet=disable" % constants.rootfs_label%disk_label_suffix
    kernel_console_params = "console=hvc0"

    if "xen-pciback.hide" in host_config:
        common_kernel_params += " %s" % host_config["xen-pciback.hide"]

    if diskutil.is_iscsi(primary_disk):
        common_kernel_params += " rd.iscsi.ibft=1 rd.iscsi.firmware=1"

    if diskutil.is_raid(primary_disk):
        common_kernel_params += " rd.auto"

    e = bootloader.MenuEntry(hypervisor="/boot/xen.efi",
                             hypervisor_args=' '.join([common_xen_params, common_xen_unsafe_params, xen_mem_params, "console=vga vga=mode-0x0311"]),
                             kernel="/boot/vmlinuz-%s-xen" % short_version,
                             kernel_args=' '.join([common_kernel_params, kernel_console_params, "console=tty0 quiet vga=785 splash plymouth.ignore-serial-consoles"]),
                             initrd="/boot/initrd-%s-xen.img" % short_version, title=MY_PRODUCT_BRAND,
                             root=constants.rootfs_label%disk_label_suffix)
    e.entry_format = Grub2Format.XEN_BOOT
    boot_config.append("xe", e)
    boot_config.default = "xe"
    if serial:
        xen_serial_params = "%s console=%s,vga" % (serial.xenFmt(), serial.port)

        e = bootloader.MenuEntry(hypervisor="/boot/xen.efi",
                                 hypervisor_args=' '.join([xen_serial_params, common_xen_params, common_xen_unsafe_params, xen_mem_params]),
                                 kernel="/boot/vmlinuz-%s-xen" % short_version,
                                 kernel_args=' '.join([common_kernel_params, "console=tty0", kernel_console_params]),
                                 initrd="/boot/initrd-%s-xen.img" % short_version, title=MY_PRODUCT_BRAND+" (Serial)",
                                 root=constants.rootfs_label%disk_label_suffix)
        e.entry_format = Grub2Format.XEN_BOOT
        boot_config.append("xe-serial", e)
        if boot_serial:
            boot_config.default = "xe-serial"
        e = bootloader.MenuEntry(hypervisor="/boot/xen.efi",
                                 hypervisor_args=' '.join([safe_xen_params, common_xen_params, xen_serial_params]),
                                 kernel="/boot/vmlinuz-%s-xen" % short_version,
                                 kernel_args=' '.join(["earlyprintk=xen", common_kernel_params, "console=tty0", kernel_console_params]),
                                 initrd="/boot/initrd-%s-xen.img" % short_version, title=MY_PRODUCT_BRAND+" in Safe Mode",
                                 root=constants.rootfs_label%disk_label_suffix)
        e.entry_format = Grub2Format.XEN_BOOT
        boot_config.append("safe", e)

    e = bootloader.MenuEntry(hypervisor="", hypervisor_args="", kernel="/boot/memtest86+x64.efi",
                            kernel_args="",
                            initrd="", title="Memtest86+ (UEFI)",
                            root=constants.rootfs_label%disk_label_suffix)
    e.entry_format = Grub2Format.LINUX
    boot_config.append("memtest", e)

    e = bootloader.MenuEntry(hypervisor="/boot/xen-fallback.efi",
                             hypervisor_args=' '.join([common_xen_params, common_xen_unsafe_params, xen_mem_params]),
                             kernel="/boot/vmlinuz-fallback",
                             kernel_args=' '.join([common_kernel_params, kernel_console_params, "console=tty0"]),
                             initrd="/boot/initrd-fallback.img",
                             title="%s (Xen %s / Linux %s)" % (MY_PRODUCT_BRAND, xen_version, xen_kernel_version),
                             root=constants.rootfs_label%disk_label_suffix)
    e.entry_format = Grub2Format.XEN_BOOT
    boot_config.append("fallback", e)
    if serial:
        e = bootloader.MenuEntry(hypervisor="/boot/xen-fallback.efi",
                                 hypervisor_args=' '.join([xen_serial_params, common_xen_params, common_xen_unsafe_params, xen_mem_params]),
                                 kernel="/boot/vmlinuz-fallback",
                                 kernel_args=' '.join([common_kernel_params, "console=tty0", kernel_console_params]),
                                 initrd="/boot/initrd-fallback.img",
                                 title="%s (Serial, Xen %s / Linux %s)" % (MY_PRODUCT_BRAND, xen_version, xen_kernel_version),
                                 root=constants.rootfs_label%disk_label_suffix)
        e.entry_format = Grub2Format.XEN_BOOT
        boot_config.append("fallback-serial", e)

def installBootLoader(mounts, disk, boot_partnum, primary_partnum, branding,
                      disk_label_suffix, location, write_boot_entry, install_type, serial=None,
                      boot_serial=None, host_config=None):
    assert(location in [constants.BOOT_LOCATION_MBR, constants.BOOT_LOCATION_PARTITION])

    if host_config:
        s = serial and {'port': serial.id, 'baud': int(serial.baud)} or None

        fn = os.path.join(mounts['boot'], "efi/EFI/xenserver/grub.cfg")
        boot_config = bootloader.Bootloader('grub2', fn,
                                            timeout=constants.BOOT_MENU_TIMEOUT,
                                            serial=s, location=location)
        xen_version = getXenVersion(mounts['root'])
        if xen_version is None:
            raise RuntimeError("Unable to determine Xen version.")
        xen_kernel_version = getKernelVersion(mounts['root'])
        if not xen_kernel_version:
            raise RuntimeError("Unable to determine kernel version.")
        buildBootLoaderMenu(mounts, xen_version, xen_kernel_version, boot_config,
                            serial, boot_serial, host_config, disk,
                            disk_label_suffix)
        util.assertDir(os.path.dirname(fn))
        boot_config.commit()

    root_partition = partitionDevice(disk, primary_partnum)
    if write_boot_entry:
        setEfiBootEntry(mounts, disk, boot_partnum, install_type, branding)

def setEfiBootEntry(mounts, disk, boot_partnum, install_type, branding):
    def check_efibootmgr_err(rc, err, install_type, err_type):
        if rc != 0:
            if install_type in (INSTALL_TYPE_REINSTALL, INSTALL_TYPE_RESTORE):
                logger.error("%s: %s" % (err_type, err))
            else:
                raise RuntimeError("%s: %s" % (err_type, err))

    # First remove existing entries
    rc, out, err = util.runCmd2(["chroot", mounts['root'], "/usr/sbin/efibootmgr"], True, True)
    check_efibootmgr_err(rc, err, install_type, "Failed to list efi boot entries")

    # This list ensures that upgrades from previous versions with different
    # names work, and the current version (so that self-upgrades always work).
    labels = '|'.join(['XenServer', 'Citrix Hypervisor', branding['product-brand']])
    for line in out.splitlines():
        match = re.match("Boot([0-9a-fA-F]{4})\\*? +(?:%s)$" % (labels,), line)
        if match:
            bootnum = match.group(1)
            rc, err = util.runCmd2(["chroot", mounts['root'], "/usr/sbin/efibootmgr",
                                    "--delete-bootnum", "--bootnum", bootnum], with_stderr=True)
            check_efibootmgr_err(rc, err, install_type,
                                 "Failed to remove efi boot entry %r" % (line,))

    # Then add a new one
    if os.path.exists(os.path.join(mounts['esp'], 'EFI/xenserver/shimx64.efi')):
        efi = "EFI/xenserver/shimx64.efi"
    elif os.path.exists(os.path.join(mounts['esp'], 'EFI/xenserver/grubx64.efi')):
        efi = "EFI/xenserver/grubx64.efi"
    else:
        raise RuntimeError("Failed to find EFI loader")
    rc, err = util.runCmd2(["chroot", mounts['root'], "/usr/sbin/efibootmgr", "-c",
                            "-L", branding['product-brand'], "-l", '\\' + efi.replace('/', '\\'),
                            "-d", disk, "-p", str(boot_partnum)], with_stderr=True)
    check_efibootmgr_err(rc, err, install_type, "Failed to add new efi boot entry")

##########
# mounting and unmounting of various volumes

def mountVolumes(primary_disk, boot_partnum, primary_partnum, logs_partnum, cleanup):
    mounter = DeviceMounter()
    mounter.mount()

    mounts = {'root': '/tmp/root',
              'boot': '/tmp/root/boot'}

    rootp = partitionDevice(primary_disk, primary_partnum)
    util.assertDir('/tmp/root')
    util.mount(rootp, mounts['root'])
    rc, out = util.runCmd2(['cat', '/proc/mounts'], with_stdout=True)
    logger.log(out)
    tool = PartitionTool(primary_disk)
    logs_partition = tool.getPartition(logs_partnum)

    util.assertDir(constants.EXTRA_SCRIPTS_DIR)
    util.mount('tmpfs', constants.EXTRA_SCRIPTS_DIR, ['size=2m'], 'tmpfs')
    util.assertDir(os.path.join(mounts['root'], 'mnt'))
    util.bindMount(constants.EXTRA_SCRIPTS_DIR, os.path.join(mounts['root'], 'mnt'))
    new_cleanup = cleanup + [ ("umount-/tmp/root", util.umount, (mounts['root'], )),
                              ("umount-/tmp/root/mnt",  util.umount, (os.path.join(mounts['root'], 'mnt'), )) ]

    for d in ('proc', 'sys', 'dev'):
        mountdir = os.path.join(mounts['root'], d)
        util.assertDir(mountdir)
        util.bindMount("/%s" % d, mountdir)
        new_cleanup.append(("umount-%s" % mountdir,  util.umount, mountdir, ))

    mountdir = os.path.join(mounts['root'], 'tmp')
    util.assertDir(mountdir)
    util.mount('none', mountdir, None, 'tmpfs')
    new_cleanup.append(("umount-%s" % mountdir,  util.umount, mountdir, ))

    mounts['esp'] = '/tmp/root/boot/efi'
    bootp = partitionDevice(primary_disk, boot_partnum)
    util.assertDir(os.path.join(mounts['root'], 'boot', 'efi'))
    util.mount(bootp, mounts['esp'])
    new_cleanup.append(("umount-/tmp/root/boot/efi", util.umount, (mounts['esp'], )))

    mountdir = os.path.join(mounts['root'], "sys/firmware/efi/efivars")
    util.bindMount("/sys/firmware/efi/efivars", mountdir)
    new_cleanup.append(("umount-/tmp/root/sys/firmware/efi/efivars", util.umount, (mountdir, )))
    if logs_partition:
        mounts['logs'] = os.path.join(mounts['root'], 'var/log')
        util.assertDir(mounts['logs'])
        util.mount(partitionDevice(primary_disk, logs_partnum), mounts['logs'])
        new_cleanup.append(("umount-/tmp/root/var/log", util.umount, (mounts['logs'], )))

    return mounts, new_cleanup

def umountVolumes(mounts, cleanup, force=False):
    def filterCleanup(tag, _, __):
        return (not tag.startswith("umount-%s" % mounts['root']) and
                not tag.startswith("umount-%s" % os.path.join(mounts['root'], 'mnt')) and
                not tag.startswith("umount-%s" % mounts['boot']))

    util.umount(os.path.join(mounts['root'], 'mnt'))
    util.umount(constants.EXTRA_SCRIPTS_DIR)
    if 'esp' in mounts:
        util.umount(mounts['esp'])
        util.umount(os.path.join(mounts['root'], "sys/firmware/efi/efivars"))
    if 'logs' in mounts:
        util.umount(mounts['logs'])

    util.umount(os.path.join(mounts['root'], 'tmp'))

    for d in ('proc', 'sys', 'dev'):
        util.umount(os.path.join(mounts['root'], d))

    util.umount(mounts['root'])
    cleanup = list(filter(filterCleanup, cleanup))
    return cleanup

##########
# second stage install helpers:

def writeKeyboardConfiguration(mounts, keymap):
    util.assertDir("%s/etc/sysconfig/" % mounts['root'])
    if not keymap:
        keymap = 'us'
        logger.log("No keymap specified, defaulting to 'us'")

    vconsole = open("%s/etc/vconsole.conf" % mounts['root'], 'w')
    vconsole.write("KEYMAP=%s\n" % keymap)
    vconsole.close()

def prepareSwapfile(mounts, primary_disk, swap_partnum, disk_label_suffix):

    tool = PartitionTool(primary_disk)

    swap_partition = tool.getPartition(swap_partnum)

    if swap_partition:
        dev = partitionDevice(primary_disk, swap_partnum)
        while True:
            # The uuid of a swap partition overlaps the same position as the
            # superblock magic for a MINIX filesystem (offset 0x410 or 0x418).
            # The uuid might by coincidence match the superblock magic. The
            # magic is only two bytes long and there are several different
            # magic identifiers which increases the chances of matching.  If
            # this happens, blkid marks the partition as ambivalent because it
            # contains multiple signatures which prevents by-label symlinks
            # from being created and the swap partition from being activated.
            # Avoid this by running mkswap until the filesystem is no longer
            # ambivalent.
            util.runCmd2(['chroot', mounts['root'], 'mkswap', '-L', constants.swap_label%disk_label_suffix, dev])
            rc, out = util.runCmd2(['chroot', mounts['root'], 'blkid', '-o', 'udev', '-p', dev], with_stdout=True)
            keys = [line.strip().split('=')[0] for line in out.strip().split('\n')]
            if 'ID_FS_AMBIVALENT' not in keys:
                break
    else:
        util.assertDir("%s/var/swap" % mounts['root'])
        util.runCmd2(['dd', 'if=/dev/zero',
                      'of=%s' % os.path.join(mounts['root'], constants.swap_file.lstrip('/')),
                      'bs=1024', 'count=%d' % (constants.swap_file_size * 1024)])
        util.runCmd2(['chroot', mounts['root'], 'mkswap', constants.swap_file])

def writeFstab(mounts, primary_disk, logs_partnum, swap_partnum, disk_label_suffix, fs_type):

    tool = PartitionTool(primary_disk)
    swap_partition = tool.getPartition(swap_partnum)
    logs_partition = tool.getPartition(logs_partnum)

    fstab = open(os.path.join(mounts['root'], 'etc/fstab'), "w")
    fstab.write("LABEL=%s    /         %s     defaults   1  1\n" % (rootfs_label%disk_label_suffix, fs_type))
    fstab.write("LABEL=%s    /boot/efi         %s     defaults   0  2\n" % (bootfs_label%disk_label_suffix.upper(), bootfs_type))

    if swap_partition:
        fstab.write("LABEL=%s          swap      swap   defaults   0  0\n" % constants.swap_label%disk_label_suffix)
    else:
        if os.path.exists(os.path.join(mounts['root'], constants.swap_file.lstrip('/'))):
            fstab.write("%s          swap      swap   defaults   0  0\n" % (constants.swap_file))
    if logs_partition:
        fstab.write("LABEL=%s    /var/log         %s     defaults   0  2\n" % (logsfs_label%disk_label_suffix, fs_type))

def enableAgent(mounts, network_backend, services):
    if network_backend == constants.NETWORK_BACKEND_VSWITCH:
        util.runCmd2(['chroot', mounts['root'],
                      'systemctl', 'enable',
                                   'openvswitch.service',
                                   'openvswitch-xapi-sync.service'])

    util.assertDir(os.path.join(mounts['root'], constants.BLOB_DIRECTORY))

    # Enable/disable miscellaneous services
    actMap = {'enabled': 'enable', 'disabled': 'disable'}
    for (service, state) in services.items():
        action = 'disable' if constants.CC_PREPARATIONS and state is None else actMap.get(state)
        if action:
            util.runCmd2(['chroot', mounts['root'], 'systemctl', action, service + '.service'])

def configureCC(mounts):
    '''Tailor the installation for Common Criteria mode.'''

    if not constants.CC_PREPARATIONS:
        return

    # Turn on SSL certificate verification.
    open(os.path.join(mounts['root'], 'var/lib/xcp/verify_certificates'), 'wb').close()

    if util.runCmd2(['chroot', mounts['root'],
                     'systemctl', 'is-enabled', 'sshd.service']) == 0:
        ssh_rule = '-A INPUT -i xenbr0 -p tcp -m tcp --dport 22 -m state --state NEW -j ACCEPT'
    else:
        ssh_rule = ''

    with open(CC_FIREWALL_CONF, 'rb') as conf:
        rules = conf.read()
    with open(os.path.join(mounts['root'], 'etc', 'sysconfig', 'iptables'), 'wb') as out:
        out.write(rules.replace(b'@SSH_RULE@', ssh_rule.encode()))

def writeResolvConf(mounts, hn_conf, ns_conf):
    (manual_hostname, hostname) = hn_conf
    (manual_nameservers, nameservers) = ns_conf

    if manual_hostname:
        # 'search' option in resolv.conf
        try:
            dot = hostname.index('.')
            if dot + 1 != len(hostname):
                resolvconf = open("%s/etc/resolv.conf" % mounts['root'], 'w')
                dname = hostname[dot + 1:]
                resolvconf.write("search %s\n" % dname)
                resolvconf.close()
        except:
            pass
    else:
        hostname = ''

    # /etc/hostname:
    eh = open('%s/etc/hostname' % mounts['root'], 'w')
    eh.write(hostname + "\n")
    eh.close()


    if manual_nameservers:

        resolvconf = open("%s/etc/resolv.conf" % mounts['root'], 'a')
        for ns in nameservers:
            if ns != "":
                resolvconf.write("nameserver %s\n" % ns)
        resolvconf.close()

def writeMachineID(mounts):
    util.bindMount("/dev", "%s/dev" % mounts['root'])

    try:
        # Remove any existing machine-id file
        try:
            os.unlink(os.path.join(mounts['root'], 'etc/machine-id'))
        except:
            pass
        util.runCmd2(['chroot', mounts['root'], 'systemd-machine-id-setup'])
    finally:
        util.umount("%s/dev" % mounts['root'])

def setTimeZone(mounts, tz):
    # make the localtime link:
    assert util.runCmd2(['ln', '-sf', '../usr/share/zoneinfo/%s' % tz,
                         '%s/etc/localtime' % mounts['root']]) == 0

def setRootPassword(mounts, root_pwd):
    # avoid using shell here to get around potential security issues.  Also
    # note that chpasswd needs -m to allow longer passwords to work correctly
    # but due to a bug in the RHEL5 version of this tool it segfaults when this
    # option is specified, so we have to use passwd instead if we need to
    # encrypt the password.  Ugh.
    (pwdtype, root_password) = root_pwd
    if pwdtype == 'pwdhash':
        cmd = ["/usr/sbin/chroot", mounts["root"], "chpasswd", "-e"]
        pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     close_fds=True,
                                     universal_newlines=True)
        pipe.communicate('root:%s\n' % root_password)
        assert pipe.wait() == 0
    else:
        cmd = ["/usr/sbin/chroot", mounts['root'], "passwd", "--stdin", "root"]
        pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,
                                     close_fds=True,
                                     universal_newlines=True)
        pipe.communicate(root_password + "\n")
        assert pipe.wait() == 0

# write /etc/sysconfig/network-scripts/* files
def configureNetworking(mounts, admin_iface, admin_bridge, admin_config, hn_conf, ns_conf, nethw, preserve_settings, network_backend):
    """ Writes configuration files that the firstboot scripts will consume to
    configure interfaces via the CLI.  Writes a loopback device configuration.
    to /etc/sysconfig/network-scripts, and removes any other configuration
    files from that directory."""

    (manual_hostname, hostname) = hn_conf
    (manual_nameservers, nameservers) = ns_conf
    domain = None
    if manual_hostname:
        dot = hostname.find('.')
        if dot != -1:
            domain = hostname[dot+1:]

    # always set network backend
    util.assertDir(os.path.join(mounts['root'], 'etc/xensource'))
    nwconf = open("%s/etc/xensource/network.conf" % mounts["root"], "w")
    nwconf.write("%s\n" % network_backend)
    logger.log("Writing %s to /etc/xensource/network.conf" % network_backend)
    nwconf.close()

    util.assertDir(os.path.join(mounts['root'], constants.FIRSTBOOT_DATA_DIR))
    mgmt_conf_file = os.path.join(mounts['root'], constants.FIRSTBOOT_DATA_DIR, 'management.conf')
    if not os.path.exists(mgmt_conf_file):
        mc = open(mgmt_conf_file, 'w')
        print("LABEL='%s'" % admin_iface, file=mc)
        print("MODE='%s'" % netinterface.NetInterface.getModeStr(admin_config.mode), file=mc)
        if admin_config.mode == netinterface.NetInterface.Static:
            print("IP='%s'" % admin_config.ipaddr, file=mc)
            print("NETMASK='%s'" % admin_config.netmask, file=mc)
            if admin_config.gateway:
                print("GATEWAY='%s'" % admin_config.gateway, file=mc)
            if manual_nameservers:
                print("DNS='%s'" % (','.join(nameservers),), file=mc)
            if domain:
                print("DOMAIN='%s'" % domain, file=mc)
        print("MODEV6='%s'" % netinterface.NetInterface.getModeStr(admin_config.modev6), file=mc)
        if admin_config.modev6 == netinterface.NetInterface.Static:
            print("IPv6='%s'" % admin_config.ipv6addr, file=mc)
            if admin_config.ipv6_gateway:
                print("IPv6_GATEWAY='%s'" % admin_config.ipv6_gateway, file=mc)
        if admin_config.vlan:
            print("VLAN='%d'" % admin_config.vlan, file=mc)
        mc.close()

    if network_backend == constants.NETWORK_BACKEND_VSWITCH:
        # CA-51684: blacklist bridge module
        bfd = open("%s/etc/modprobe.d/blacklist-bridge.conf" % mounts["root"], "w")
        bfd.write("install bridge /bin/true\n")
        bfd.close()

    if preserve_settings:
        return

    # Clean install only below this point


    network_scripts_dir = os.path.join(mounts['root'], 'etc/sysconfig/network-scripts')

    # remove any files that may be present in the filesystem already,
    # particularly those created by kudzu:
    network_scripts = os.listdir(network_scripts_dir)
    for s in network_scripts:
        if s.startswith('ifcfg-'):
            os.unlink(os.path.join(network_scripts_dir, s))

    # write the configuration file for the loopback interface
    lo = open(os.path.join(network_scripts_dir, 'ifcfg-lo'), 'w')
    lo.write("DEVICE=lo\n")
    lo.write("IPADDR=127.0.0.1\n")
    lo.write("NETMASK=255.0.0.0\n")
    lo.write("NETWORK=127.0.0.0\n")
    lo.write("BROADCAST=127.255.255.255\n")
    lo.write("ONBOOT=yes\n")
    lo.write("NAME=loopback\n")
    lo.close()

    save_dir = os.path.join(mounts['root'], constants.FIRSTBOOT_DATA_DIR, 'initial-ifcfg')
    util.assertDir(save_dir)

    # now we need to write /etc/sysconfig/network
    nfd = open("%s/etc/sysconfig/network" % mounts["root"], "w")
    nfd.write("NETWORKING=yes\n")
    if admin_config.modev6:
        nfd.write("NETWORKING_IPV6=yes\n")
        util.runCmd2(['chroot', mounts['root'], 'systemctl', 'enable', 'ip6tables'])
    else:
        nfd.write("NETWORKING_IPV6=no\n")
        netutil.disable_ipv6_module(mounts["root"])
    nfd.write("IPV6_AUTOCONF=no\n")
    nfd.write('NTPSERVERARGS="iburst prefer"\n')
    nfd.close()

    # EA-1069 - write static-rules.conf and dynamic-rules.conf
    if not os.path.exists(os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/.from_install/')):
        os.makedirs(os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/.from_install/'), 0o775)

    netutil.static_rules.path = os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/static-rules.conf')
    netutil.static_rules.save()
    netutil.static_rules.path = os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/.from_install/static-rules.conf')
    netutil.static_rules.save()

    netutil.dynamic_rules.path = os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/dynamic-rules.json')
    netutil.dynamic_rules.save()
    netutil.dynamic_rules.path = os.path.join(mounts['root'], 'etc/sysconfig/network-scripts/interface-rename-data/.from_install/dynamic-rules.json')
    netutil.dynamic_rules.save()

def writeXencommons(controlID, mounts):
    with open(os.path.join(mounts['root'], constants.XENCOMMONS_FILE), "r") as f:
        contents = f.read()

    dom0_uuid_str = ("XEN_DOM0_UUID=%s" % controlID)
    contents = re.sub('.*XEN_DOM0_UUID=.*', dom0_uuid_str, contents)

    with open(os.path.join(mounts['root'], constants.XENCOMMONS_FILE), "w") as f:
        f.write(contents)

def writeInventory(installID, controlID, mounts, primary_disk, backup_partnum, logs_partnum, boot_partnum, swap_partnum,
                   storage_partnum, guest_disks, admin_bridge, branding, admin_config, host_config, install_type):
    inv = open(os.path.join(mounts['root'], constants.INVENTORY_FILE), "w")
    if 'product-brand' in branding:
       inv.write("PRODUCT_BRAND='%s'\n" % branding['product-brand'])
    if PRODUCT_NAME:
       inv.write("PRODUCT_NAME='%s'\n" % PRODUCT_NAME)
    if 'product-version' in branding:
       inv.write("PRODUCT_VERSION='%s'\n" % branding['product-version'])
    if PRODUCT_VERSION_TEXT:
       inv.write("PRODUCT_VERSION_TEXT='%s'\n" % PRODUCT_VERSION_TEXT)
    if PRODUCT_VERSION_TEXT_SHORT:
       inv.write("PRODUCT_VERSION_TEXT_SHORT='%s'\n" % PRODUCT_VERSION_TEXT_SHORT)
    if COMPANY_NAME:
       inv.write("COMPANY_NAME='%s'\n" % COMPANY_NAME)
    if COMPANY_NAME_SHORT:
       inv.write("COMPANY_NAME_SHORT='%s'\n" % COMPANY_NAME_SHORT)
    if COMPANY_PRODUCT_BRAND:
       inv.write("COMPANY_PRODUCT_BRAND='%s'\n" % COMPANY_PRODUCT_BRAND)
    if BRAND_CONSOLE:
       inv.write("BRAND_CONSOLE='%s'\n" % BRAND_CONSOLE)
    if BRAND_CONSOLE_URL:
       inv.write("BRAND_CONSOLE_URL='%s'\n" % BRAND_CONSOLE_URL)
    inv.write("PLATFORM_NAME='%s'\n" % branding['platform-name'])
    inv.write("PLATFORM_VERSION='%s'\n" % branding['platform-version'])

    layout = 'ROOT'
    if backup_partnum > 0:
        layout += ',BACKUP'
    if logs_partnum > 0:
        layout += ',LOG'
    if boot_partnum > 0:
        layout += ',BOOT'
    if swap_partnum > 0:
        layout += ',SWAP'
    if storage_partnum > 0:
        layout += ',SR'
    inv.write("PARTITION_LAYOUT='%s'\n" % layout)

    if 'product-build' in branding:
        inv.write("BUILD_NUMBER='%s'\n" % branding['product-build'])
    inv.write("INSTALLATION_DATE='%s'\n" % str(datetime.datetime.now()))
    inv.write("PRIMARY_DISK='%s'\n" % (diskutil.idFromPartition(primary_disk) or primary_disk))
    if backup_partnum > 0:
        inv.write("BACKUP_PARTITION='%s'\n" % (diskutil.idFromPartition(partitionDevice(primary_disk, backup_partnum)) or partitionDevice(primary_disk, backup_partnum)))
    inv.write("INSTALLATION_UUID='%s'\n" % installID)
    inv.write("CONTROL_DOMAIN_UUID='%s'\n" % controlID)
    inv.write("DOM0_MEM='%d'\n" % host_config['dom0-mem'])
    inv.write("DOM0_VCPUS='%d'\n" % host_config['dom0-vcpus'])
    inv.write("MANAGEMENT_INTERFACE='%s'\n" % admin_bridge)
    # Default to IPv4 unless we have only got an IPv6 admin interface
    if ((not admin_config.mode) and admin_config.modev6):
        inv.write("MANAGEMENT_ADDRESS_TYPE='IPv6'\n")
    else:
        inv.write("MANAGEMENT_ADDRESS_TYPE='IPv4'\n")
    if constants.CC_PREPARATIONS and install_type == constants.INSTALL_TYPE_FRESH:
        inv.write("CC_PREPARATIONS='true'\n")
    inv.close()

def touchSshAuthorizedKeys(mounts):
    util.assertDir("%s/root/.ssh/" % mounts['root'])
    fh = open("%s/root/.ssh/authorized_keys" % mounts['root'], 'a')
    fh.close()

def writeDMVSelections(mounts, selected_multiversion_drivers):
    # select the default driver variants
    if len(selected_multiversion_drivers) == 0:
        logger.log("we got empty driver variants selection.")

        dmv_data_provider = dmvutil.getDMVData()
        # if we got empty selection we need more log data to see devices and
        # drivers that we have
        drivers = dmv_data_provider.getDriversData()
        dmvutil.logDriverVariants(drivers)

        selected_variants = dmv_data_provider.chooseDefaultDriverVariants()
        for variant in selected_variants:
            selected_multiversion_drivers.append((variant.drvname, variant.oemtype))
        logger.log("pass default driver variants to driver-tool.")

    for driver_name, variant_name in selected_multiversion_drivers:
        logger.log("write variant %s selection for driver %s." % (variant_name, driver_name))

        cmdparams = ['driver-tool', '-s', '-n', driver_name, '-v', variant_name]
        chrootcmd = ['chroot', mounts['root']]
        chrootcmd.extend(cmdparams)
        util.runCmd2(chrootcmd, with_stdout=True)

################################################################################
# OTHER HELPERS

# This function is not supposed to throw exceptions so that it can be used
# within the main exception handler.
def writeLog(primary_disk, primary_partnum, logs_partnum):
    tool = PartitionTool(primary_disk)

    logs_partition = tool.getPartition(logs_partnum)

    if logs_partition:
        try:
            bootnode = partitionDevice(primary_disk, logs_partnum)
            primary_fs = util.TempMount(bootnode, 'install-')
            try:
                log_location = os.path.join(primary_fs.mount_point, "installer")
                if os.path.islink(log_location):
                    log_location = os.path.join(primary_fs.mount_point, os.readlink(log_location).lstrip("/"))
                util.assertDir(log_location)
                xelogging.collectLogs(log_location, os.path.join(primary_fs.mount_point,"root"))
            except:
                pass
            primary_fs.unmount()
        except:
            pass
    else:
        try:
            bootnode = partitionDevice(primary_disk, primary_partnum)
            primary_fs = util.TempMount(bootnode, 'install-')
            try:
                log_location = os.path.join(primary_fs.mount_point, "var/log/installer")
                if os.path.islink(log_location):
                    log_location = os.path.join(primary_fs.mount_point, os.readlink(log_location).lstrip("/"))
                util.assertDir(log_location)
                xelogging.collectLogs(log_location, os.path.join(primary_fs.mount_point,"root"))
            except:
                pass
            primary_fs.unmount()
        except:
            pass

def writei18n(mounts):
    path = os.path.join(mounts['root'], 'etc/locale.conf')
    fd = open(path, 'w')
    fd.write('LANG="en_US.UTF-8"\n')
    fd.close()

def verifyRepos(sources, ui):
    """ Check repos are accessible """

    with DeviceMounter():
        for i in sources:
            repo_good = False

            if ui:
                if tui.repo.check_repo_def((i['media'], i['address']), False) == tui.repo.REPOCHK_NO_ERRORS:
                    repo_good = True
            else:
                try:
                    repos = repository.repositoriesFromDefinition(i['media'], i['address'])
                    if len(repos) > 0:
                        repo_good = True
                except:
                    pass

            if not repo_good:
                raise RuntimeError("Unable to access repository (%s, %s)" % (i['media'], i['address']))

def getUpgrader(source):
    """ Returns an appropriate upgrader for a given source. """
    return upgrade.getUpgrader(source)

def prepareTarget(progress_callback, upgrader, *args):
    return upgrader.prepareTarget(progress_callback, *args)

def doBackup(progress_callback, upgrader, *args):
    return upgrader.doBackup(progress_callback, *args)

def prepareUpgrade(progress_callback, upgrader, *args):
    """ Gets required state from existing installation. """
    return upgrader.prepareUpgrade(progress_callback, *args)

def completeUpgrade(upgrader, *args):
    """ Puts back state into new filesystem. """
    return upgrader.completeUpgrade(*args)

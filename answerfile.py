# SPDX-License-Identifier: GPL-2.0-only

"""answerfile - parse installation answerfiles"""

from constants import *
import diskutil
import disktools
from netinterface import *
import netutil
import product
import scripts
import util
import xml.dom.minidom

from xcp import logger
from xcp.xmlunwrap import *

def normalize_disk(disk):
    if disk.startswith('iscsi:'):
        # An rfc4173 spec identifying a LUN in the iBFT.  We
        # should be logged into this already.  Convert this spec into a
        # disk location.
        return diskutil.rfc4173_to_disk(disk)

    if not disk.startswith('/dev/'):
        disk = '/dev/' + disk
    return diskutil.partitionFromId(disk)

class AnswerfileException(Exception):
    pass

class Answerfile:

    def __init__(self, xmldoc):
        self.top_node = xmldoc.documentElement
        if self.top_node.nodeName in ['installation', 'upgrade']:
            self.operation = 'installation'
        elif self.top_node.nodeName == 'restore':
            self.operation = 'restore'
        else:
            raise AnswerfileException("Unexpected top level element")

    @staticmethod
    def fetch(location):
        logger.log("Fetching answerfile from %s" % location)
        util.fetchFile(location, ANSWERFILE_PATH)

        try:
            xmldoc = xml.dom.minidom.parse(ANSWERFILE_PATH)
        except:
            raise AnswerfileException("Answerfile is incorrectly formatted.")

        return Answerfile(xmldoc)

    @staticmethod
    def generate(location):
        ret, out, err = scripts.run_script(location, 'answerfile')
        if ret != 0:
            raise AnswerfileException("Generator script failed:\n\n%s" % err)

        try:
            xmldoc = xml.dom.minidom.parseString(out)
        except:
            raise AnswerfileException("Generator script returned incorrectly formatted output.")

        return Answerfile(xmldoc)

    def processAnswerfileSetup(self):
        """Process enough of the answerfile so that disks can be made available
        for inspection."""

        logger.log("Processing XML answerfile setup.")
        results = {}
        results.update(self.parseDriverSource())
        results.update(self.parseDriverMultiVersion())
        results.update(self.parseUIConfirmationPrompt())

        return results

    def processAnswerfile(self):
        logger.log("Processing XML answerfile for %s." % self.operation)
        if self.operation == 'installation':
            install_type = getStrAttribute(self.top_node, ['mode'], default='fresh')
            if install_type == "fresh":
                results = self.parseFreshInstall()
            elif install_type == "reinstall":
                results = self.parseReinstall()
            elif install_type == "upgrade":
                results = self.parseUpgrade()
            else:
                raise AnswerfileException("Unknown mode, %s" % install_type)

            results.update(self.parseCommon())
        elif self.operation == 'restore':
            results = self.parseRestore()

        return results

    def parseScripts(self):

        def buildURL(stype, path):
            if stype == 'nfs' and not path.startswith('nfs://'):
                return 'nfs://'+path
            return path

        # new format
        script_nodes = getElementsByTagName(self.top_node, ['script'])
        for node in script_nodes:
            stage = getStrAttribute(node, ['stage'], mandatory=True).lower()
            stype = getStrAttribute(node, ['type'], mandatory=True).lower()
            script = buildURL(stype, getText(node))
            scripts.add_script(stage, script)

        # deprecated formats
        nodes = getElementsByTagName(self.top_node, ['post-install-script'])
        if len(nodes) == 1:
            stype = getStrAttribute(nodes[0], ['type'], mandatory=False).lower()
            script = buildURL(stype, getText(nodes[0]))
            scripts.add_script('filesystem-populated', script)
        nodes = getElementsByTagName(self.top_node, ['install-failed-script'])
        if len(nodes) == 1:
            stype = getStrAttribute(nodes[0], ['type'], mandatory=False).lower()
            script = buildURL(stype, getText(nodes[0]))
            scripts.add_script('installation-complete', script)
        return {}

    def parseFreshInstall(self):
        results = {}

        results['install-type'] = INSTALL_TYPE_FRESH
        results['preserve-settings'] = False
        results['backup-existing-installation'] = False

        results.update(self.parseDisks())
        results.update(self.parseInterface())
        results.update(self.parseRootPassword())
        results.update(self.parseNSConfig())
        self.parseTimeConfig(results)
        results.update(self.parseKeymap())
        results.update(self.parseServices())

        return results

    def parseReinstall(self):
        # identical to fresh install except backup existing
        results = self.parseFreshInstall()
        results['backup-existing-installation'] = True
        return results

    def parseUpgrade(self):
        results = {}

        results['install-type'] = INSTALL_TYPE_REINSTALL
        results['preserve-settings'] = True
        results['backup-existing-installation'] = True
        results.update(self.parseExistingInstallation())

        return results

    def parseRestore(self):
        results = {}

        results['install-type'] = INSTALL_TYPE_RESTORE

        backups = product.findXenSourceBackups()
        if len(backups) == 0:
            raise AnswerfileException("Could not locate exsisting backup.")

        results['backups'] = backups
        logger.log("Backup list: %s" % ", ".join(str(b) for b in backups))
        nodes = getElementsByTagName(self.top_node, ['backup-disk'])
        if len(nodes) == 1:
            disk = normalize_disk(getText(nodes[0]))
            disk = disktools.getMpathMasterOrDisk(disk)
            logger.log("Filtering backup list for disk %s" % disk)
            backups = [x for x in backups if x.root_disk == disk]
            logger.log("Backup list filtered: %s" % ", ".join(str(b) for b in backups))

        if len(backups) > 1:
            logger.log("Multiple backups found. Aborting...")
            raise AnswerfileException("Multiple backups were found. Unable to deduce which backup to restore from.")
        elif len(backups) == 0:
            logger.log("Unable to find a backup to restore. Aborting...")
            raise AnswerfileException("Unable to find a backup to restore.")

        logger.log("Restoring backup %s." % str(backups[0]))
        results['backup-to-restore'] = backups[0]

        return results

    def parseCommon(self):
        results = {};

        results.update(self.parseSource())

        nodes = getElementsByTagName(self.top_node, ['network-backend'])
        if len(nodes) > 0:
            network_backend = getText(nodes[0])
            if network_backend == NETWORK_BACKEND_BRIDGE:
                results['network-backend'] = NETWORK_BACKEND_BRIDGE
            elif network_backend in [NETWORK_BACKEND_VSWITCH, NETWORK_BACKEND_VSWITCH_ALT]:
                results['network-backend'] = NETWORK_BACKEND_VSWITCH

        nodes = getElementsByTagName(self.top_node, ['bootloader'])
        if len(nodes) > 0:
            results['bootloader-location'] = getMapAttribute(nodes[0], ['location'],
                                                             [('mbr', BOOT_LOCATION_MBR),
                                                              ('partition', BOOT_LOCATION_PARTITION)],
                                                             default='mbr')

            results['write-boot-entry'] = getBoolAttribute(nodes[0], ['write-boot-entry'], default=True)

            bl = getText(nodes[0])
            if bl not in ['' , 'grub2']:
                raise AnswerfileException("Unsupported bootloader '%s'" % bl)

        return results

    def parseExistingInstallation(self):
        results = {}

        inst = getElementsByTagName(self.top_node, ['existing-installation'],
                                    mandatory=True)
        disk = normalize_disk(getText(inst[0]))
        logger.log("Normalized disk: %s" % disk)
        disk = disktools.getMpathMasterOrDisk(disk)
        logger.log('Primary disk: ' + disk)
        results['primary-disk'] = disk

        results['fs-type'] = getStrAttribute(inst[0], ['fs-type'], default=default_rootfs_type)
        if results['fs-type'] not in allowed_rootfs_types:
            raise AnswerfileException("Expected fs-type to be one of %s" % (", ".join(allowed_rootfs_types)))

        installations = product.findXenSourceProducts()
        installations = [x for x in installations if x.primary_disk == disk or diskutil.idFromPartition(x.primary_disk) == disk]
        if len(installations) == 0:
            raise AnswerfileException("Could not locate the installation specified to be reinstalled.")
        elif len(installations) > 1:
            # FIXME non-multipath case?
            logger.log("Warning: multiple paths detected - recommend use of --device_mapper_multipath=yes")
            logger.log("Warning: selecting 1st path from %s" % str([x.primary_disk for x in installations]))
        results['installation-to-overwrite'] = installations[0]
        return results

    def parseSource(self):
        results = {'sources': []}
        sources = getElementsByTagName(self.top_node, ['source'], mandatory=True)

        for i in sources:
            rtype = getStrAttribute(i, ['type'], mandatory=True)

            if rtype == 'local':
                address = "Install disc"
            elif rtype in ['url', 'nfs']:
                address = getText(i)
            else:
                raise AnswerfileException("Invalid type for <source> media specified.")
            if rtype == 'url' and address.startswith('nfs://'):
                rtype = 'nfs'
                address = address[6:]

            if rtype == 'url':
                address = util.URL(address)

            results['sources'].append({'media': rtype, 'address': address})

        return results

    def parseDriverSource(self):
        results = {}
        for source in getElementsByTagName(self.top_node, ['driver-source']):
            if 'extra-repos' not in results:
                results['extra-repos'] = []

            rtype = getStrAttribute(source, ['type'], mandatory=True)
            if rtype == 'local':
                address = "Install disc"
            elif rtype in ['url', 'nfs']:
                address = getText(source)
            else:
                raise AnswerfileException("Invalid type for <driver-source> media specified.")
            if rtype == 'url' and address.startswith('nfs://'):
                rtype = 'nfs'
                address = address[6:]

            if rtype == 'url':
                address = util.URL(address)

            results['extra-repos'].append((rtype, address))
        return results

    def parseDriverMultiVersion(self):
        results = {}
        results['selected-multiversion-drivers'] = []
        elements = getElementsByTagName(self.top_node, ['selected-multiversion-drivers'])
        if len(elements) > 0:
            node = elements[0]
            drivers = node.getElementsByTagName("driver")
            for driver in drivers:
                drvname = driver.getAttribute("name")
                variant = driver.firstChild.nodeValue if driver.firstChild else ""
                results['selected-multiversion-drivers'].append((drvname, variant))
        return results

    def parseDisks(self):
        results = {}

        # Primary disk (installation)
        node = getElementsByTagName(self.top_node, ['primary-disk'], mandatory=True)[0]
        results['preserve-first-partition'] = \
                                            getMapAttribute(node, ['preserve-first-partition'],
                                                            [('true', 'true'),
                                                             ('yes', 'true'),
                                                             ('false', 'false'),
                                                             ('no', 'false'),
                                                             ('if-utility', PRESERVE_IF_UTILITY)],
                                                            default='if-utility')
        primary_disk = normalize_disk(getText(node))
        results['primary-disk'] = primary_disk

        inc_primary = getBoolAttribute(node, ['guest-storage', 'gueststorage'],
                                       default=True)
        results['sr-at-end'] = getBoolAttribute(node, ['sr-at-end'], default=True)
        results['fs-type'] = getStrAttribute(node, ['fs-type'], default=default_rootfs_type)
        if results['fs-type'] not in allowed_rootfs_types:
            raise AnswerfileException("Expected fs-type to be one of %s" % (", ".join(allowed_rootfs_types)))

        # Guest disk(s) (Local SR)
        guest_disks = set()
        if inc_primary:
            guest_disks.add(primary_disk)
        for node in getElementsByTagName(self.top_node, ['guest-disk']):
            guest_disks.add(normalize_disk(getText(node)))
        results['sr-on-primary'] = results['primary-disk'] in guest_disks
        results['guest-disks'] = list(guest_disks)

        sr_type_mapping = []
        for sr_type in SR_TYPE_LVM, SR_TYPE_EXT, SR_TYPE_LARGE_BLOCK:
            if sr_type:
                sr_type_mapping.append((sr_type, sr_type))

        large_block_disks = [
            disk
            for disk in guest_disks
            if diskutil.isLargeBlockDisk(disk)
        ]

        if SR_TYPE_LARGE_BLOCK and len(large_block_disks) > 0:
            default_sr_type = SR_TYPE_LARGE_BLOCK
        else:
            default_sr_type = SR_TYPE_LVM

        sr_type = getMapAttribute(self.top_node,
                                  ['sr-type', 'srtype'],
                                  sr_type_mapping,
                                  default=default_sr_type)

        if SR_TYPE_LARGE_BLOCK and sr_type != SR_TYPE_LARGE_BLOCK and len(large_block_disks) > 0:
            raise AnswerfileException("%s not compatible with SR type %s"
                                      % (", ".join(large_block_disks), sr_type))

        results['sr-type'] = sr_type

        return results

    def parseInterface(self):
        results = {}
        node = getElementsByTagName(self.top_node, ['admin-interface'], mandatory=True)[0]
        nethw = netutil.scanConfiguration()
        if_hwaddr = None

        if_name = getStrAttribute(node, ['name'])
        if if_name and if_name in nethw:
            if_hwaddr = nethw[if_name].hwaddr
        else:
            if_hwaddr = getStrAttribute(node, ['hwaddr'])
            if if_hwaddr:
                matching_list = [x for x in list(nethw.values()) if x.hwaddr == if_hwaddr.lower()]
                if len(matching_list) == 1:
                    if_name = matching_list[0].name
        if not if_name and not if_hwaddr:
             raise AnswerfileException("<admin-interface> tag must have one of 'name' or 'hwaddr'")

        results['net-admin-interface'] = if_name

        proto = getStrAttribute(node, ['proto'], mandatory=True)
        if proto == 'static':
            ip = getText(getElementsByTagName(node, ['ip', 'ipaddr'], mandatory=True)[0])
            subnet = getText(getElementsByTagName(node, ['subnet-mask', 'subnet'], mandatory=True)[0])
            gateway = getText(getElementsByTagName(node, ['gateway'], mandatory=True)[0])
            results['net-admin-configuration'] = NetInterface(NetInterface.Static, if_hwaddr, ip, subnet, gateway, dns=None)
        elif proto == 'dhcp':
            results['net-admin-configuration'] = NetInterface(NetInterface.DHCP, if_hwaddr)
        else:
            results['net-admin-configuration'] = NetInterface(None, if_hwaddr)

        protov6 = getStrAttribute(node, ['protov6'])
        if protov6 == 'static':
            ipv6 = getText(getElementsByTagName(node, ['ipv6'], mandatory=True)[0])
            gatewayv6 = getText(getElementsByTagName(node, ['gatewayv6'], mandatory=True)[0])
            results['net-admin-configuration'].addIPv6(NetInterface.Static, ipv6, gatewayv6)
        elif protov6 == 'dhcp':
            results['net-admin-configuration'].addIPv6(NetInterface.DHCP)
        elif protov6 == 'autoconf':
            results['net-admin-configuration'].addIPv6(NetInterface.Autoconf)

        vlan = getStrAttribute(node, ['vlan'])
        if vlan:
            if not netutil.valid_vlan(vlan):
                raise AnswerfileException("Invalid value for vlan attribute specified.")
            results['net-admin-configuration'].vlan = int(vlan)

        if not results['net-admin-configuration'].valid():
            raise AnswerfileException("<admin-interface> tag must have IPv4 or IPv6 defined.")
        return results

    def parseRootPassword(self):
        results = {}
        nodes = getElementsByTagName(self.top_node, ['root-password'])
        if len(nodes) > 0:
            pw_type = getMapAttribute(nodes[0], ['type'], [('plaintext', 'plaintext'),
                                                           ('hash', 'pwdhash')],
                                      default='plaintext')
            results['root-password'] = (pw_type, getText(nodes[0]))
        return results

    def parseNSConfig(self):
        results = {}
        nodes = getElementsByTagName(self.top_node, ['name-server', 'nameserver'])
        results['manual-nameservers'] = (len(nodes) > 0, [getText(x) for x in nodes])
        nodes = getElementsByTagName(self.top_node, ['hostname'])
        if len(nodes) > 0:
            results['manual-hostname'] = (True, getText(nodes[0]))
        else:
            results['manual-hostname'] = (False, None)
        return results

    def parseTimeConfig(self, results):

        nodes = getElementsByTagName(self.top_node, ['timezone'])
        if len(nodes) > 0:
            results['timezone'] = getText(nodes[0])
        else:
            # Default to Etc/UTC if not present
            results['timezone'] = 'Etc/UTC'

        ntpNodes = getElementsByTagName(self.top_node, ['ntp'])
        ntpServerNodes = getElementsByTagName(self.top_node, ['ntp-server', 'ntp-servers'])
        if len(ntpNodes) == 1:
            results['ntp-config-method'] = getStrAttribute(ntpNodes[0], ['source'], mandatory=True)
            if results['ntp-config-method'] not in ("dhcp", "default", "manual", "none"):
                raise AnswerfileException("Expected <ntp> source to be one of (dhcp, default, manual, none)")
        elif len(ntpNodes) == 0:
            # Maintain backwards compatibility by matching the missing ntp element to the answerfile contents
            results['ntp-config-method'] = "default"
            if ntpServerNodes:
                results['ntp-config-method'] = "manual"
            elif results['net-admin-configuration'].mode == NetInterface.DHCP:
                results['ntp-config-method'] = "dhcp"
        else:
            raise AnswerfileException("Expected zero or one <ntp> elements but found %s" % len(ntpNodes))

        if ntpServerNodes and results['ntp-config-method'] != "manual":
            raise AnswerfileException("<ntp-server> and <ntp-servers> elements are only valid when using <ntp source=\"manual\" />")

        results['ntp-servers'] = map(lambda x: getText(x), ntpServerNodes)

    def parseKeymap(self):
        results = {}
        nodes = getElementsByTagName(self.top_node, ['keymap'])
        if len(nodes) > 0:
            results['keymap'] = getText(nodes[0])
        return results

    def parseUIConfirmationPrompt(self):
        results = {}
        nodes = getElementsByTagName(self.top_node, ['ui-confirmation-prompt'])
        if len(nodes) > 0:
            results['ui-confirmation-prompt'] = bool(getText(nodes[0]))
        return results

    def parseServices(self):
        results = {}
        services = {}
        serviceNodes = getElementsByTagName(self.top_node, ['service'])
        servicesSeen = set()
        for sn in serviceNodes:
            service = getStrAttribute(sn, ['name'], mandatory=True)
            if service in servicesSeen:
                raise AnswerfileException("Multiple entries for service %s" % service)
            servicesSeen.add(service)
            state = getStrAttribute(sn, ['state'], mandatory=True)
            if not state in ('enabled', 'disabled'):
                raise AnswerfileException("Invalid state for service %s: %s" % (service, state))
            services[service] = state
        if services:
             # replace the default value
             results['services'] = services
        return results

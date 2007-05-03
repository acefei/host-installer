import tui.installer.screens 
import tui.progress
import uicontroller
import hardware
import netutil
import repository
import constants

from snack import *

def runMainSequence(results, ram_warning, vt_warning, installed_products):
    """ Runs the main installer sequence and updtes results with a
    set of values ready for the backend. """
    uis = tui.installer.screens
    Step = uicontroller.Step

    def ask_preserve_settings_predicate(answers):
        return answers['install-type'] == constants.INSTALL_TYPE_REINSTALL and \
               answers.has_key('installation-to-overwrite') and \
               answers['installation-to-overwrite'].settingsAvailable()

    netifs = netutil.getNetifList()
    has_multiple_nics = lambda _: len(netifs) > 1
    if len(netifs) == 1:
        results['net-admin-interface'] = netifs[0]

    is_reinstall_fn = lambda a: a['install-type'] == constants.INSTALL_TYPE_REINSTALL
    is_clean_install_fn = lambda a: a['install-type'] == constants.INSTALL_TYPE_FRESH
    is_using_remote_media_fn = lambda a: a['source-media'] in ['url', 'nfs']

    def not_preserve_settings(answers):
        return not answers.has_key('preserve-settings') or \
               not answers['preserve-settings']

    def local_media_predicate(answers):
        return answers.has_key('source-media') and \
               answers['source-media'] == 'local'

    if len(installed_products) == 0:
        results['install-type'] = constants.INSTALL_TYPE_FRESH
        results['preserve-settings'] = False

    seq = [
        Step(uis.welcome_screen),
        Step(uis.eula_screen),
        Step(uis.hardware_warnings,
             args=[ram_warning, vt_warning],
             predicates=[lambda _:(ram_warning or vt_warning)]),
        Step(uis.get_installation_type, args=[installed_products],
             predicates=[lambda _:len(installed_products) > 0]),
        Step(uis.ask_preserve_settings,
             predicates=[ask_preserve_settings_predicate]),
        Step(uis.backup_existing_installation,
             predicates=[is_reinstall_fn]),
        Step(uis.select_primary_disk,
             predicates=[is_clean_install_fn]),
        Step(uis.select_guest_disks,
             predicates=[is_clean_install_fn]),
        Step(uis.confirm_erase_volume_groups,
             predicates=[is_clean_install_fn]),
        Step(uis.select_installation_source),
        Step(uis.use_extra_media, args=[vt_warning],
             predicates=[local_media_predicate]),
        Step(uis.setup_runtime_networking, 
             predicates=[is_using_remote_media_fn]),
        Step(uis.get_source_location,
             predicates=[is_using_remote_media_fn]),
        Step(uis.verify_source),
        Step(uis.get_root_password,
             predicates=[not_preserve_settings]),
        Step(uis.get_timezone_region,
             predicates=[not_preserve_settings]),
        Step(uis.get_timezone_city,
             predicates=[not_preserve_settings]),
        Step(uis.get_time_configuration_method,
             predicates=[not_preserve_settings]),
        Step(uis.get_ntp_servers,
             predicates=[not_preserve_settings]),
        Step(uis.get_admin_interface,
             predicates=[not_preserve_settings, has_multiple_nics]),
        Step(uis.get_admin_interface_configuration,
             predicates=[not_preserve_settings]),
        Step(uis.get_name_service_configuration,
             predicates=[not_preserve_settings]),
        Step(uis.confirm_installation),
        ]
    return uicontroller.runSequence(seq, results)

def more_media_sequence(installed_repo_ids):
    """ Displays the sequence of screens required to load additional
    media to install from.  installed_repo_ids is a list of repository
    IDs of repositories we already installed from, to help avoid
    issues where multiple CD drives are present.

    Returns pair: (install more, then ask again)"""
    def get_more_media(_):
        """ 'Please insert disk' dialog. """
        done = False
        while not done:
            more = tui.progress.OKDialog("New Media", "Please insert your extra disc now.", True)
            if more == "cancel":
                # they hit cancel:
                rv = -1
                done = True
            else:
                # they hit OK - check there is a disc
                repos = repository.repositoriesFromDefinition('local', '')
                if len(repos) == 0:
                    ButtonChoiceWindow(
                        tui.screen, "Error",
                        "No installation files were found - please check your disc and try again.",
                        ['Back'])
                else:
                    # found repositories - can leave this screen
                    rv = 1
                    done = True
        return rv

    def confirm_more_media(_):
        """ 'Really use this disc?' screen. """
        repos = repository.repositoriesFromDefinition('local', '')
        assert len(repos) > 0

        media_contents = []
        for r in repos:
            if r.identifier() in installed_repo_ids:
                media_contents.append(" * %s (already installed)" % r.name())
            else:
                media_contents.append(" * %s" % r.name())
        text = "The media you have inserted contains:\n\n" + "\n".join(media_contents)

        done = False
        while not done:
            ans = ButtonChoiceWindow(tui.screen, "New Media", text, ['Use media', 'Verify media', 'Back'], width=50)
            
            if ans == 'verify media':
                tui.installer.screens.interactive_source_verification('local', '')
            elif ans == 'back':
                rc = -1
                done = True
            else:
                rc = 1
                done = True

        return rc

    seq = [ uicontroller.Step(get_more_media), uicontroller.Step(confirm_more_media) ]
    direction = uicontroller.runSequence(seq, {})
    return (direction == 1, False)

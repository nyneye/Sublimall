# -*- coding:utf-8 -*-
import os
import time
import shutil
import sublime
import zipfile

from urllib.parse import urljoin
from sublime_plugin import ApplicationCommand

from .command import CommandWithStatus

from ..logger import logger
from ..archiver import Archiver
from ..utils import generate_temp_filename
from .. import requests
from .. import SETTINGS_USER_FILE


class RetrieveCommand(ApplicationCommand, CommandWithStatus):

    def __init__(self, *args, **kwargs):
        super(RetrieveCommand, self).__init__(*args, **kwargs)
        self.stream = None
        self.running = False
        self.password = None
        self.archive_filename = None

    def _package_control_has_packages(self):
        """
        Returns whether Package Control is installed or not
        """
        pc_settings = sublime.load_settings('Package Control.sublime-settings')
        installed_packages = pc_settings.get('installed_packages', [])
        return bool(installed_packages)

    def post_unpack(self):
        """
        Resets values
        """
        self.unset_message()
        os.unlink(self.archive_filename)
        self.stream = None
        self.running = False
        self.password = None
        self.archive_filename = None

    def abort(self):
        """
        Aborts unpacking
        """
        self.set_message("No password supplied : aborting.")
        logger.warn('No password supplied. Aborting...')
        self.post_unpack()

    def prompt_password(self):
        """
        Shows an input panel for entering password
        """
        sublime.active_window().show_input_panel(
            "Enter archive password",
            initial_text='',
            on_done=self.check_zipfile,
            on_cancel=self.abort,
            on_change=None
        )

    def check_zipfile(self, password=None, first_try=False):
        """
        Check if stream is an unprotected zipfile
        If retry set to True, loop one more time
        """
        # Try opening the zipfile to check password
        try:
            if password is not None:
                self.set_message("Trying to decrypt your archive. Please wait.")
                self.zf.setpassword(password.encode())
            self.zf.testzip()
        except RuntimeError:
            if first_try:
                self.set_message("Archive is protected. Please enter password.")
                logger.info('Archive is protected with a passphrase')
            else:
                self.set_message("Wrong password")
                logger.info('Bad passphrase')
            self.prompt_password()
        else:
            self.password = password
            self.zf.close()
            self.set_message("Archive decrypted. Applying configuration.")
            sublime.set_timeout_async(self.unpack, 0)

    def retrieve_from_server(self):
        """
        Retrieve archive from API
        """
        data = {
            'version': sublime.version()[:1],
            'email': self.email,
            'api_key': self.api_key,
        }

        self.set_message("Requesting archive...")
        try:
            r = requests.post(
                url=self.api_retrieve_url,
                data=data,
                stream=True,
                timeout=self.settings.get('http_upload_timeout'))
        except requests.exceptions.ConnectionError as err:
            self.set_timed_message(
                "Error while retrieving archive: server not available, try later",
                clear=True)
            self.running = False
            logger.error(
                'Server (%s) not available, try later.\n'
                '==========[EXCEPTION]==========\n'
                '%s\n'
                '===============================' % (
                    self.api_retrieve_url, err))
            return

        if r.status_code == 200:
            logger.info('HTTP [%s] Downloading archive' % r.status_code)

            # Create a temporary file and write response content in it
            self.archive_filename = generate_temp_filename()
            logger.info(
                'Create temporary file and write response content in it (%s)' %
                self.archive_filename)
            self.stream = open(self.archive_filename, 'wb')
            shutil.copyfileobj(r.raw, self.stream)
            self.stream.close()

            # Had some mysterious problems using in memory stream so re-open file instead
            self.zf = zipfile.ZipFile(self.archive_filename, 'r')
            self.check_zipfile(first_try=True)
        elif r.status_code == 403:
            self.set_timed_message(
                "Error while requesting archive: wrong credentials", clear=True)
            logger.info('HTTP [%s] Bad credentials' % r.status_code)
        elif r.status_code == 404:
            self.set_timed_message(
                "Error while requesting archive: version %s not found" %
                sublime.version(),
                clear=True)
            logger.error("HTTP [%s] %s" (r.status_code, r.content))
        else:
            msg = "Unexpected error (HTTP STATUS: %s)" % r.status_code
            try:
                j = r.json()
                for error in j.get('errors'):
                    msg += " - %s" % error
            except:
                pass
            self.set_timed_message(msg, clear=True, time=10)
            logger.error("HTTP [%s] %s" % (r.status_code, r.content))

        self.running = False

    def backup(self):
        """
        Creates a backup of Packages and Installed Packages before unpacking
        """
        archiver = Archiver()
        backup_dir_path = os.path.join(
            sublime.packages_path(),
            'Sublimall',
            'Backup')
        backup_path = os.path.join(backup_dir_path, '%s.zip' % time.time())
        logger.info('Create backup in Sublimall %s of actual configuration' % backup_path)
        try:
            if not os.path.exists(backup_dir_path):
                os.makedirs(backup_dir_path)
            archiver.pack_packages(output_filename=backup_path, backup=True)
        except Exception as err:
            self.set_timed_message(str(err), clear=True)
            raise

        return True

    def unpack(self):
        """
        Unpack archive
        """
        # Backup installed packages in Sublimall backups dir
        self.backup()

        # Move packages directories to a .bak
        self.set_message(u"Moving directories...")
        archiver = Archiver()
        archiver.move_packages_to_backup_dirs()

        # Unpack backup
        self.set_message(u"Extracting archive...")
        logger.info('Extracting archive')
        archiver.unpack_packages(self.archive_filename, password=self.password)

        # Delete moved directories
        self.set_message(u"Deleting old directories...")
        archiver.remove_backup_dirs()

        self.set_message(u"Your Sublime Text has been synced !")
        logger.info('Finised')

        if self._package_control_has_packages():
            message_lines = [
                "Package Control has been detected.",
                "If your configuration is not yet fully loaded, please restart "
                "Sublimeall and wait for Package Control to install missing packages.",
                "You might have to restart Sublime another time if "
                "themes are not correctly loaded."
            ]
        else:
            message_lines = [
                "Sync done.",
                "Please restart Sublime Text if your configuration is not fully loaded.",
            ]
        sublime.message_dialog('\n'.join(message_lines))

        self.post_unpack()

    def start(self):
        """
        Decrypt stream using private key
        """
        self.running = True
        self.retrieve_from_server()

    def run(self, *args):
        if self.running:
            self.set_timed_message("Already working on a backup...")
            logger.warn('Already working on a backup')
            return

        self.settings = sublime.load_settings(SETTINGS_USER_FILE)
        self.email = self.settings.get('email', '')
        self.api_key = self.settings.get('api_key', '')

        if not self.email or not self.api_key:
            self.set_timed_message(
                "api_key or email is missing in your Sublimall configuration", clear=True)
            logger.warn('API key or email is missing in configuration file. Abort')
            return

        self.api_retrieve_url = urljoin(
            self.settings.get('api_root_url'), self.settings.get('api_retrieve_url'))

        sublime.set_timeout_async(self.start, 0)

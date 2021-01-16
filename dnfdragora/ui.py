
'''
dnfdragora is a graphical frontend based on rpmdragora implementation
that uses dnf as rpm backend, due to libyui python bindings dnfdragora
is able to comfortably behave like a native gtk or qt5 or ncurses application

License: GPLv3

Author:  Angelo Naselli <anaselli@linux.it>

@package dnfdragora
'''

import os
import sys
import platform
import datetime
import re
import yui
import webbrowser
from queue import SimpleQueue, Empty
from enum import Enum
from inspect import ismethod

#NOTE we need a glib.MainLoop in TUI and use threading for it
import threading
from gi.repository import GLib
import dnfdaemon.client

import manatools.ui.helpdialog as helpdialog
import dnfdragora.basedragora
import dnfdragora.compsicons as compsicons
import dnfdragora.groupicons as groupicons
import dnfdragora.progress_ui as progress_ui
import dnfdragora.dialogs as dialogs
import dnfdragora.misc as misc
import dnfdragora.helpinfo as helpinfo

import dnfdragora.config
from dnfdragora import const

import gettext
import logging
logger = logging.getLogger('dnfdragora.ui')

class UIError(Exception):
  'Raise an Error from UI'
  def __init__(self, msg=None):
    self.msg = msg

  def __str__(self):
    if self.msg:
      return self.msg
    else:
      return ""

class DNFDragoraStatus(Enum):
    '''
    Enum
        STARTUP Starting
        LOCKING Lock requested
        RUNNING Locked, normal running no requests
    '''
    STARTUP = 1
    LOCKING = 2
    CACHING_UPDATE = 3
    CACHING_INSTALLED = 4
    CACHING_AVAILABLE = 5
    RUN_TRANSACTION = 6
    RUNNING = 7



class PackageQueue:
    '''
    A Queue class to store selected packages/groups and the pending actions
    '''

    def __init__(self):
        self.packages = {}
        self._setup_packages()
        self.actions = {}


#QUEUE_PACKAGE_TYPES = {
#    'i': 'install',
#    'u': 'update',
#    'r': 'remove',
#    'o': 'obsolete',
#    'ri': 'reinstall',
#    'do': 'downgrade',
#    'li': 'localinstall'
#}
    def _setup_packages(self):
        for key in const.QUEUE_PACKAGE_TYPES:
            self.packages[key] = []

    def clear(self):
        del self.packages
        self.packages = {}
        self._setup_packages()
        self.actions = {}


    def get(self, action=None):
        if action is None:
            return self.packages
        else:
            return self.packages[action]

    def total(self):
        num = 0
        for key in const.QUEUE_PACKAGE_TYPES:
            num += len(self.packages[key])
        return num

    def add(self, pkg, action):
        """Add a package to queue"""
        pkg_id = pkg.pkg_id
        if pkg_id in self.actions.keys():
            old_action = self.actions[pkg_id]
            if old_action != action:
                self.packages[old_action].remove(pkg_id)
                if (pkg.installed and action != 'i' or not pkg.installed and action != 'r'):
                    self.packages[action].append(pkg_id)
                    self.actions[pkg_id] = action
                else:
                    del self.actions[pkg_id]
        else:
            self.packages[action].append(pkg_id)
            self.actions[pkg_id] = action

    def checked(self, pkg):
        '''
        returns if a package has to be checked in gui package-list
        '''
        pkg_id = pkg.pkg_id
        if pkg_id in self.actions.keys():
            return pkg.installed and self.actions[pkg_id] != 'r' or self.actions[pkg_id] != 'r'
        return pkg.installed

    def action(self, pkg):
        '''
        returns the action of the queued package or None if package is not queued
        '''
        pkg_id = pkg.pkg_id
        if pkg_id in self.actions.keys():
            return self.actions[pkg_id]
        return None

    def remove(self, pkg):
        """Remove package from queue"""
        pkg_id = pkg.pkg_id
        if pkg_id in self.actions.keys():
            action = self.actions[pkg_id]
            self.packages[action].remove(pkg_id)
            del self.actions[pkg_id]


#################
# class mainGui #
#################
class mainGui(dnfdragora.basedragora.BaseDragora):
    """
    Main class
    """

    def __init__(self, options={}):
        '''
        constructor
        '''

        self._status = DNFDragoraStatus.STARTUP
        self._beforeLockAgain = 20 # 20 x 500 ms = 10 sec
        self.running = False
        self.loop_has_finished = False
        self.options = options
        self._progressBar = None
        self.packageQueue = PackageQueue()
        self.toRemove = []
        self.toInstall = []
        self.itemList = {}
        self.appname = "dnfdragora"
        self._selPkg = None
        self.md_update_interval = 48 # check any 48 hours as default
        self.md_last_refresh_date = None
        self._runtime_option_managed = False
        # TODO... _package_name, _gpg_confirm imported from old event management
        # Try to remove them when fixing progress bar
        self._package_name = None
        self._action_name = None
        self._gpg_confirm = None
        self._files_to_download = 0
        self._files_downloaded = 0
        # ...TODO

        self._transaction_tries = 0

        # {
        #   name-epoch_version-release.arch : { pkg: dnf-pkg, item: YItem}
        # }
        self.groupList = {}
        # {
        #    localized_name = { "item" : item, "name" : groupName }
        # }

        self.infoshown = {
            'updateinfo' : { 'title' : _("Update information"), 'show' : False },
            'files' : { 'title' : _("File list"), 'show' : False },
            'changelog' : { 'title' : _("Changelog"), 'show' : False },
            'requirements' : { 'title' : _("Requirements"), 'show' : False },
            }
        self.use_comps = False
        self.group_icon_path = None
        self.images_path = '/usr/share/dnfdragora/images/'
        self.always_yes = False
        self.match_all = False
        self.newest_only = False
        self.all_updates_filter = False
        self.log_enabled = False
        self.log_directory = None
        self.level_debug = False
        self.upgrades_as_updates = True # NOTE consider package to upgrade as update
        self.config = dnfdragora.config.AppConfig(self.appname)

        # settings from configuration file first
        self._configFileRead()

        if self.log_enabled:
          if self.log_directory:
            log_filename = os.path.join(self.log_directory, "dnfdragora.log")
            if self.level_debug:
              misc.logger_setup(log_filename, loglvl=logging.DEBUG)
            else:
              misc.logger_setup(log_filename)
            print("Logging into %s, debug mode is %s"%(self.log_directory, ("enabled" if self.level_debug else "disabled")))
            logger.info("dnfdragora started")
        else:
           print("Logging disabled")

        # overrides settings from comand line
        if 'group_icons_path' in self.options.keys() :
            self.group_icon_path = self.options['group_icons_path']
        if 'images_path' in self.options.keys() :
            self.images_path = self.options['images_path']
        self.update_only = 'update_only' in self.options.keys()

        if self.use_comps and not self.group_icon_path:
            self.group_icon_path = '/usr/share/pixmaps/comps/'

        # Adding / as last har in the path if not present
        if not self.images_path.endswith('/'):
            self.images_path += "/"
        if self.group_icon_path and not self.group_icon_path.endswith('/'):
            self.group_icon_path += "/"

        if yui.YUI.app().isTextMode():
          self.glib_loop = GLib.MainLoop()
          self.glib_thread = threading.Thread(target=self.glib_mainloop, args=(self.glib_loop,))
          self.glib_thread.start()


        dnfdragora.basedragora.BaseDragora.__init__(self, self.use_comps)

        # setup UI
        self._setupUI()

        self._enableAction(False)
        self.pbar_layout.setEnabled(True)

        self.backend
        self.dialog.pollEvent()
        self.find_entry.setKeyboardFocus()



    def glib_mainloop(self, loop):
      '''
      thread function for glib main loop
      '''
      loop.run()

    def _configFileRead(self) :
        '''
        reads the configuration file and sets application data
        '''

        if self.config.systemSettings :
            settings = {}
            if 'settings' in self.config.systemSettings.keys() :
                settings = self.config.systemSettings['settings']

            if 'use_comps' in settings.keys() :
                self.use_comps = settings['use_comps']

            if 'always_yes' in settings.keys() :
                self.always_yes = settings['always_yes']

            if 'log_directory' in settings.keys() :
              print("Warning logging must be set in user preferences, discarded")

            if 'log_level_debug' in settings.keys() :
              print("Warning logging must be set in user preferences, discarded")

            # config['settings']['path']
            path_settings = {}
            if 'path' in settings.keys():
                path_settings = settings['path']
            if 'group_icons' in path_settings.keys():
                self.group_icon_path = path_settings['group_icons']
            if 'images' in path_settings.keys():
                self.images_path = path_settings['images']

            # config['settings']['search']
            search = {}
            if 'search' in settings.keys():
                search = settings['search']
            if 'match_all' in search.keys():
                self.match_all = search['match_all']
            if 'newest_only' in search.keys():
                self.newest_only = search['newest_only']

            # all_updates force first running without user setting configured with the view
            # all packages and to_updates filter, fedora users coming from yumex-dnf are used to
            # get this view. Mageia and OpenMandriva Groups and All instead
            if 'all_updates' in settings.keys():
              self.all_updates_filter = settings['all_updates']

        # User preferences overriding
        user_settings = {}
        if self.config.userPreferences:
            if 'settings' in self.config.userPreferences.keys() :
                if self.config.userPreferences['settings'] is None:
                    self.config.userPreferences['settings'] = {}
                user_settings = self.config.userPreferences['settings']
                #### MetaData
                if 'metadata' in user_settings.keys():
                  metadata = user_settings['metadata']
                  if 'update_interval' in metadata.keys():
                    self.md_update_interval = metadata['update_interval']
                  else:
                    self.md_update_interval = metadata['update_interval'] = 48
                  if 'last_update' in metadata.keys():
                    self.md_last_refresh_date =  metadata['last_update']
                else:
                  self.config.userPreferences['settings']['metadata'] ={
                    'update_interval': self.md_update_interval, # 48 Default
                    'last_update': ''
                  }
                if 'upgrades as updates' in user_settings.keys():
                  self.upgrades_as_updates = user_settings['upgrades as updates']

                #### Search
                if 'search' in user_settings.keys():
                    search = user_settings['search']
                    if 'newest_only' in search.keys():
                        self.newest_only = search['newest_only']
                    if 'match_all' in search.keys():
                        self.match_all = search['match_all']
                #### Logging
                if 'log' in user_settings.keys():
                  log = user_settings['log']
                  if 'enabled' in log.keys() :
                    self.log_enabled = log['enabled']
                  if self.log_enabled:
                    if 'directory' in log.keys() :
                        self.log_directory = log['directory']
                    if 'level_debug' in log.keys() :
                        self.level_debug = log['level_debug']

        # metadata settings is needed adding it to update old configuration files
        if not 'settings' in self.config.userPreferences.keys() :
          self.config.userPreferences['settings'] = {}
        if not 'metadata' in self.config.userPreferences['settings'].keys():
          self.config.userPreferences['settings']['metadata'] = {
            'update_interval': self.md_update_interval, # 48 Default
            'last_update': ''
          }

    def _setupUI(self) :
        '''
            setup main dialog
        '''
        yui.YUI.app().setApplicationTitle(_("Software Management - dnfdragora"))

        self.icon = self.images_path + "dnfdragora.png"
        self.logo = self.images_path + "dnfdragora-logo.png"
        yui.YUI.app().setApplicationIcon(self.icon)

        MGAPlugin = "mga"

        self.factory = yui.YUI.widgetFactory()
        mgaFact = yui.YExternalWidgets.externalWidgetFactory(MGAPlugin)
        self.mgaFactory = yui.YMGAWidgetFactory.getYMGAWidgetFactory(mgaFact)
        self.optFactory = yui.YUI.optionalWidgetFactory()

        self.AboutDialog = dialogs.AboutDialog(self)

        ### MAIN DIALOG ###
        self.dialog = self.factory.createMainDialog()

        vbox = self.factory.createVBox(self.dialog)

        hbox_menubar = self.factory.createHBox(vbox)

        #Line for logo and title
        #hbox_iconbar  = self.factory.createHBox(vbox)
        #head_align_left  = self.factory.createLeft(hbox_iconbar)
        #hbox_iconbar     = self.factory.createHBox(head_align_left)
        #self.factory.createImage(hbox_iconbar, self.logo)

        #self.factory.createHeading(hbox_iconbar, _("Software Management"))

        hbox_top = self.factory.createHBox(vbox)
        hbox_middle = self.factory.createHBox(vbox)
        hbox_bottom = self.factory.createHBox(vbox)
        self.pbar_layout = self.factory.createHBox(vbox)
        hbox_footbar = self.factory.createHBox(vbox)

        #######
        foot_align_left = self.factory.createLeft(hbox_footbar)
        foot_align_right = self.factory.createRight(hbox_footbar)
        hbox_footbar = self.factory.createHBox(foot_align_left)
        right_footbar = self.factory.createHBox(foot_align_right)
        #######

        self.menu_layout = hbox_menubar
        self.top_layout = hbox_top
        self.middle_layout = hbox_middle
        self.bottom_layout = hbox_bottom
        self.footbar_layout = hbox_footbar

        #hbox_headbar.setWeight(1,10)
        hbox_top.setWeight(1,5)
        hbox_middle.setWeight(1,50)
        hbox_bottom.setWeight(1,30)
        hbox_footbar.setWeight(1,10)

        # Tree for groups
        self.tree = self.factory.createTree(hbox_middle, "")
        self.tree.setWeight(0,10)
        self.tree.setNotify(True)

        packageList_header = yui.YCBTableHeader()
        columns = [ _('Name'), _('Summary'), _('Version'), _('Release'), _('Arch'), _('Size')]

        checkboxed = True
        packageList_header.addColumn("", checkboxed)
        for col in (columns):
            packageList_header.addColumn(col, not checkboxed)

        if not self.update_only :
            packageList_header.addColumn(_("Status"), not checkboxed)

        self.packageList = self.mgaFactory.createCBTable(hbox_middle,packageList_header)
        self.packageList.setWeight(0,50)
        self.packageList.setImmediateMode(True)

        self.filters = {
            'all' : {'title' : _("All")},
            'installed' : {'title' : _("Installed")},
            'not_installed' : {'title' : _("Not installed")},
            'to_update' : {'title' : _("To update")}
        }
        ordered_filters = [ 'all', 'installed', 'to_update', 'not_installed' ]
        if platform.machine() == "x86_64" :
            # NOTE this should work on other architectures too, but maybe it
            #      is a nonsense, at least for i586
            self.filters['skip_other'] = {'title' : _("Show %s and noarch only") % platform.machine()}
            ordered_filters.append('skip_other')

        # TODO add backports
        self.views = {
            'all' : {'title' : _("All")},
            'groups' : {'title' : _("Groups")},
        }
        ordered_views = [ 'groups', 'all' ]

        self.view_box = self.factory.createComboBox(hbox_top,"")
        itemColl = yui.YItemCollection()

        view = {}
        settings = {}
        if self.config.userPreferences:
            if 'settings' in self.config.userPreferences.keys() :
                settings = self.config.userPreferences['settings']
            if 'view' in self.config.userPreferences.keys() :
                view = self.config.userPreferences['view']
            if 'show updates at startup' in settings.keys() :
                if settings['show updates at startup'] :
                    view['filter'] = 'to_update'
            if 'do not show groups at startup' in settings.keys() :
                if settings['do not show groups at startup'] :
                    view['show'] = 'all'
            if 'always_yes' in settings.keys() :
                self.always_yes = settings['always_yes']

        show_item = 'all' if self.update_only else \
          view['show'] if 'show' in view.keys() else 'groups'

        for v in ordered_views:
            item = yui.YItem(self.views[v]['title'])
            if show_item == v :
                item.setSelected(True)
            # adding item to views to find the item selected
            self.views[v]['item'] = item
            itemColl.push_back(item)
            item.this.own(False)

        self.view_box.addItems(itemColl)
        self.view_box.setNotify(True)
        self.view_box.setEnabled(not self.update_only)

        self.filter_box = self.factory.createComboBox(hbox_top,"")
        itemColl.clear()

        filter_item = 'to_update' if self.all_updates_filter or self.update_only \
          else view['filter'] if 'filter' in view.keys() else 'all'

        for f in ordered_filters:
            item = yui.YItem(self.filters[f]['title'])
            if filter_item == f:
                item.setSelected(True)
            # adding item to filters to find the item selected
            self.filters[f]['item'] = item
            itemColl.push_back(item)
            item.this.own(False)

        self.filter_box.addItems(itemColl)
        self.filter_box.setNotify(True)
        self.filter_box.setEnabled(not self.update_only)

        self.local_search_types = {
            'name'       : {'title' : _("in names")       },
            'description': {'title' : _("in descriptions")},
            'summary'    : {'title' : _("in summaries")   },
            'file'       : {'title' : _("in file names")  }
        }
        search_types = ['name', 'summary', 'description', 'file' ]

        self.search_list = self.factory.createComboBox(hbox_top,"")
        itemColl.clear()
        for s in search_types:
            item = yui.YItem(self.local_search_types[s]['title'])
            if s == search_types[0] :
                item.setSelected(True)
            # adding item to local_search_types to find the item selected
            self.local_search_types[s]['item'] = item
            itemColl.push_back(item)
            item.this.own(False)

        self.search_list.addItems(itemColl)
        self.search_list.setNotify(True)

        self.find_entry = self.factory.createInputField(hbox_top, "")

        self.use_regexp = self.factory.createCheckBox(hbox_top, _("Use regexp"))
        self.use_regexp.setNotify(True)

        icon_file = self.images_path + "find.png"
        self.find_button = self.factory.createIconButton(hbox_top, 'system-search', _("&Search"))
        self.find_button.setWeight(0,6)
        self.find_button.setDefaultButton(True)

        icon_file = self.images_path + "clear_22x22.png"
        self.reset_search_button = self.factory.createIconButton(hbox_top, icon_file, _("&Clear search"))
        self.reset_search_button.setWeight(0,7)
        self.find_entry.setWeight(0,10)

        self.info = self.factory.createRichText(hbox_bottom,"")
        self.info.setWeight(yui.YD_HORIZ,40)
        self.info.setWeight(yui.YD_VERT, 40)

        self.infobar = progress_ui.ProgressBar(self.dialog, self.pbar_layout)

        self.applyButton = self.factory.createIconButton(hbox_footbar,"",_("&Apply"))
        self.applyButton.setWeight(0,3)
        self.applyButton.setEnabled(False)

        self.checkAllButton = self.factory.createIconButton(hbox_footbar,"",_("Sel&ect all"))
        self.checkAllButton.setWeight(0,3)
        self.checkAllButton.setEnabled(False)

        spacing = self.factory.createHStretch(right_footbar)
        spacing.setWeight(0,3)
        self.quitButton = self.factory.createIconButton(right_footbar,"",_("&Quit"))
        self.quitButton.setWeight(0,3)

        ### BEGIN Menus #########################
        if (hasattr(self.factory, 'createMenuBar') and ismethod(getattr(self.factory, 'createMenuBar'))):
            logger.info("System has createMenuBar, using menubar")
            self.menubar = self.factory.createMenuBar(hbox_menubar)

            # building File menu
            mItem = self.menubar.addMenu(_("&File"))
            self.fileMenu = {
                'menu_name' : mItem,
                'reset_sel' : yui.YMenuItem(mItem, _("Reset the selection")),
                'reload'    : yui.YMenuItem(mItem, _("Refresh Metadata")),
                'repos'     : yui.YMenuItem(mItem, _("Repositories")),
                'sep0'      : mItem.addSeparator(),
                'quit'      : yui.YMenuItem(mItem, _("&Quit"), "application-exit"),
            }
            #Items must be "disowned"
            for k in self.fileMenu.keys():
                self.fileMenu[k].this.own(False)

            # building Information menu
            mItem = self.menubar.addMenu(_("&Information"))
            self.infoMenu = {
                'menu_name' : mItem,
                'history'   : yui.YMenuItem(mItem, _("&History")),
            }
            #Items must be "disowned"
            for k in self.infoMenu.keys():
                self.infoMenu[k].this.own(False)

            # building Options menu
            mItem = self.menubar.addMenu(_("&Options"))
            self.optionsMenu = {
                'menu_name'  : mItem,
                'user_prefs' : yui.YMenuItem(mItem, _("User preferences")),
            }
            #Items must be "disowned"
            for k in self.optionsMenu.keys():
                self.optionsMenu[k].this.own(False)

            # build Help menu
            mItem = self.menubar.addMenu(_("&Help"))
            self.helpMenu = {
                'menu_name': mItem,
                'help'     : yui.YMenuItem(mItem, _("Manual")),
                'sep0'     : mItem.addSeparator(),
                'about'    : yui.YMenuItem(mItem, _("&About"), 'dnfdragora'),
            }
            #Items must be "disowned"
            for k in self.helpMenu.keys():
                self.helpMenu[k].this.own(False)
        else:
            logger.info("System has not createMenuBar, using old menu buttons")
            self._createMenuButtons(self.factory.createHBox(self.factory.createLeft(hbox_menubar)))
        ### END Menus #########################

    def _createMenuButtons(self, headbar):
        ''' create obsolete menu buttons to allow using dnfdragora if manubar 
            is not implemented, in the case libyui-mga is old
        '''
        # build File menu
        self.fileMenu = {
            'widget'    : self.factory.createMenuButton(headbar, _("&File")),
            'reset_sel' : yui.YMenuItem(_("Reset the selection")),
            'reload'    : yui.YMenuItem(_("Refresh Metadata")),
            'repos'     : yui.YMenuItem(_("Repositories")),
            'quit'      : yui.YMenuItem(_("&Quit"), "application-exit"),
        }

        ordered_menu_lines = ['reset_sel', 'reload', 'repos', 'quit']
        for l in ordered_menu_lines :
            self.fileMenu['widget'].addItem(self.fileMenu[l])
        self.fileMenu['widget'].rebuildMenuTree();

        # build Options menu
        self.infoMenu = {
            'widget'    : self.factory.createMenuButton(headbar, _("&Information")),
            'history' : yui.YMenuItem(_("History")),
        }

        #NOTE following the same behavior to simplfy further menu entry addtion
        ordered_menu_lines = ['history']
        for l in ordered_menu_lines :
            self.infoMenu['widget'].addItem(self.infoMenu[l])
        self.infoMenu['widget'].rebuildMenuTree();

        # build Options menu
        self.optionsMenu = {
            'widget'    : self.factory.createMenuButton(headbar, _("&Options")),
            'user_prefs' : yui.YMenuItem(_("User preferences")),
        }

        #NOTE following the same behavior to simplfy further menu entry addtion
        ordered_menu_lines = ['user_prefs']
        for l in ordered_menu_lines :
            self.optionsMenu['widget'].addItem(self.optionsMenu[l])
        self.optionsMenu['widget'].rebuildMenuTree();

        # build help menu
        self.helpMenu = {
            'widget': self.factory.createMenuButton(headbar, _("&Help")),
            'help'  : yui.YMenuItem(_("Manual")),
            'about' : yui.YMenuItem(_("&About"), 'dnfdragora'),
        }
        ordered_menu_lines = ['help', 'about']
        for l in ordered_menu_lines :
            self.helpMenu['widget'].addItem(self.helpMenu[l])

        self.helpMenu['widget'].rebuildMenuTree()

    
    def _enableAction(self, value=True):
      '''
      disable ui actions but let's allow to quit
      '''
      self.menu_layout.setEnabled(value)
      self.top_layout.setEnabled(value)
      self.middle_layout.setEnabled(value)
      self.bottom_layout.setEnabled(value)
      self.footbar_layout.setEnabled(value)

    def _setStatusToItem(self, pkg, item, emit_changed=False) :
        '''
        set the status of the given package to the item so that it is shown on package list
        '''
        if self.update_only:
            return

        cellNum = item.cellCount()
        cell= item.cell(cellNum-1)
        icon = None

        status = self.packageQueue.action(pkg)
        if status:
            icon = self.images_path + const.QUEUE_PACKAGE_TYPES[status] +".png"
        else:
            #not queued
            if pkg.is_update:
                status = _("update")
                icon = self.images_path + "update.png"
            elif pkg.installed and self.backend.protected(pkg) :
                status = _("locked")
                icon = self.images_path + "protected.png"
            elif pkg.installed :
                status = _("installed")
                icon = self.images_path + "installed.png"
            else:
                status = ""
                icon = self.images_path + "available.png"
        if icon:
            cell.setLabel(status)
            cell.setIconName(icon)
            if emit_changed:
                self.packageList.cellChanged(cell)

    def _selectedPackage(self) :
        '''
        gets the selected package from package list, if any, and return the
        related package as DnfPackage
        '''
        selected_pkg = None
        sel = self.packageList.selectedItem()
        if sel :
            for pkg_name in self.itemList:
                if (self.itemList[pkg_name]['item'] == sel) :
                    selected_pkg = self.itemList[pkg_name]['pkg']
                    break

        return selected_pkg

    def _fillPackageList(self, groupName=None, filter="all") :
        '''
        fill package list filtered by group if groupName is given,
        and checks installed packages.
        Special value for groupName 'All' means all packages
        Available filters are:
        all, installed, not_installed, to_update and skip_other
        '''
        sel_pkg = self._selectedPackage()
        # reset info view
        # TODO self.info.setValue("")

        yui.YUI.app().busyCursor()

        self.itemList = {}
        # {
        #   name-epoch_version-release.arch : { pkg: dnf-pkg, item: YItem}
        # }
        if filter == 'all' or filter == 'to_update' or filter == 'skip_other':
            updates = self.backend.get_packages('updates')
            for pkg in updates :
                ## NOTE get_groups_from_package calls group caching so we try to avoid it if 'all' is selected
                insert_items = groupName and (groupName == 'All')
                if not insert_items and groupName :
                    groups_pkg = self.backend.get_groups_from_package(pkg)
                    insert_items = groupName in groups_pkg

                if insert_items :
                    skip_insert = (filter == 'skip_other' and not (pkg.arch == 'noarch' or pkg.arch == platform.machine()))
                    if not skip_insert :
                        item = yui.YCBTableItem(pkg.name , pkg.summary , pkg.version, pkg.release, pkg.arch, pkg.sizeM)
                        pkg_name = pkg.fullname
                        if sel_pkg :
                            if sel_pkg.fullname == pkg_name :
                                item.setSelected(True)
                        item.check(self.packageQueue.checked(pkg))
                        self.itemList[pkg_name] = {
                            'pkg' : pkg, 'item' : item
                            }
                        if not self.update_only:
                            item.addCell(" ")
                            self._setStatusToItem(pkg,item)
                        item.this.own(False)

        if filter == 'all' or filter == 'installed' or filter == 'skip_other':
            installed = self.backend.get_packages('installed')
            for pkg in installed :
                ## NOTE get_groups_from_package calls group caching so we try to avoid it if 'all' is selected
                insert_items = groupName and (groupName == 'All')
                if not insert_items and groupName :
                    groups_pkg = self.backend.get_groups_from_package(pkg)
                    insert_items = groupName in groups_pkg

                if insert_items :
                    skip_insert = (filter == 'skip_other' and not (pkg.arch == 'noarch' or pkg.arch == platform.machine()))
                    if not skip_insert :
                        item = yui.YCBTableItem(pkg.name , pkg.summary , pkg.version, pkg.release, pkg.arch, pkg.sizeM)
                        pkg_name = pkg.fullname
                        if sel_pkg :
                            if sel_pkg.fullname == pkg_name :
                                item.setSelected(True)
                        item.check(self.packageQueue.checked(pkg))
                        self.itemList[pkg_name] = {
                            'pkg' : pkg, 'item' : item
                            }
                        if not self.update_only:
                            item.addCell(" ")
                            self._setStatusToItem(pkg,item)
                        item.this.own(False)

        if filter == 'all' or filter == 'not_installed' or filter == 'skip_other':
            available = self.backend.get_packages('available')
            for pkg in available :
                ## NOTE get_groups_from_package calls group caching so we try to avoid it if 'all' is selected
                insert_items = groupName and (groupName == 'All')
                if not insert_items and groupName :
                    groups_pkg = self.backend.get_groups_from_package(pkg)
                    insert_items = groupName in groups_pkg

                if insert_items :
                    skip_insert = (filter == 'skip_other' and not (pkg.arch == 'noarch' or pkg.arch == platform.machine()))
                    if not skip_insert :
                        item = yui.YCBTableItem(pkg.name , pkg.summary , pkg.version, pkg.release, pkg.arch, pkg.sizeM)
                        pkg_name = pkg.fullname
                        if sel_pkg :
                            if sel_pkg.fullname == pkg_name :
                                item.setSelected(True)
                        item.check(self.packageQueue.checked(pkg))
                        self.itemList[pkg_name] = {
                            'pkg' : pkg, 'item' : item
                            }
                        if not self.update_only:
                            item.addCell(" ")
                            self._setStatusToItem(pkg,item)
                        item.this.own(False)

        keylist = sorted(self.itemList.keys())
        v = []
        for key in keylist :
            item = self.itemList[key]['item']
            v.append(item)

        #NOTE workaround to get YItemCollection working in python
        itemCollection = yui.YItemCollection(v)

        self.packageList.startMultipleChanges()
        # cleanup old changed items since we are removing all of them
        self.packageList.setChangedItem(None)
        self.packageList.deleteAllItems()
        self.packageList.addItems(itemCollection)
        self.packageList.doneMultipleChanges()

        yui.YUI.app().normalCursor()

    def _viewNameSelected(self):
        '''
        return the view_box name index from the selected view
        '''
        sel = self.view_box.selectedItem()
        view = 'groups'
        ordered_views = [ 'groups', 'all' ]
        for v in ordered_views:
            if self.views[v]['item'] == sel :
                return v

        return view

    def _filterNameSelected(self) :
        '''
        return the filter name index from the selected filter e.g.
        'all', 'installed', 'to_update', 'not_installed' or 'skip other'
        '''
        filter = 'all'
        sel = self.filter_box.selectedItem()
        if sel:
            for k in self.filters.keys():
                if self.filters[k]['item'] == sel:
                    return k

        return filter

    def _groupNameFromItem(self, group, treeItem) :
        '''
        return the group name to be used for a search by group
        '''
        # TODO check type yui.YTreeItem?
        for g in group.keys() :
            if g == 'name' or g == 'item' :
                continue
            if group[g]['item'] == treeItem :
                return group[g]['name']
            elif group[g]['item'].hasChildren():
                gName =  self._groupNameFromItem(group[g], treeItem)
                if gName :
                    return gName

        return None

    def _rebuildPackageListWithSearchGroup(self):
      '''
      Next code is used to check if the package list must be rebuilt in
      the case of an active search result
      '''
      rebuild_package_list = False
      sel = self.tree.selectedItem()
      if sel :
        group = self._groupNameFromItem(self.groupList, sel)
        if (group == "Search"):
          rebuild_package_list = not self._searchPackages()
        else:
          rebuild_package_list = True
      return rebuild_package_list

    def _fillGroupTree(self) :
        '''
        fill the group tree, look for the retrieved groups and set their icons
        from groupicons module
        '''

        self.groupList = {}
        rpm_groups = []
        yui.YUI.app().busyCursor()

        view = self._viewNameSelected()
        filter = self._filterNameSelected()

        if view != 'all' :
            print ("Start looking for groups")

            #filters = [ 'all', 'installed', 'to_update', 'not_installed' ]
            if (filter == 'to_update'):
                logger.debug("get groups for update packages only")
                pkgs = self.backend.get_packages('updates')
                for pkg in pkgs:
                    groups = self.backend.get_groups_from_package(pkg)
                    rpm_groups = list(set().union(rpm_groups, groups))
            elif (filter == 'installed'):
                logger.debug("get groups for installed packages only")
                pkgs = self.backend.get_packages('installed')
                for pkg in pkgs:
                    groups = self.backend.get_groups_from_package(pkg)
                    rpm_groups = list(set().union(rpm_groups, groups))
            elif (filter == 'not_installed'):
                logger.debug("get groups for available packages only")
                pkgs = self.backend.get_packages('available')
                for pkg in pkgs:
                    groups = self.backend.get_groups_from_package(pkg)
                    rpm_groups = list(set().union(rpm_groups, groups))
            else:
                # all pr arch
                logger.debug("get all groups")
                rpm_groups = self.backend.get_groups()
            rpm_groups = sorted(rpm_groups)
        else:
            rpm_groups = ['All']

        if not rpm_groups:
            rpm_groups = ['Empty']

        groups = self.gIcons.groups

        for g in rpm_groups:
            #X/Y/Z/...
            currG = groups
            currT = self.groupList
            subGroups = g.split("/")
            currItem = None
            parentItem = None
            groupName = None

            for sg in subGroups:
                if groupName:
                    groupName += "/%s"%(sg)
                else:
                    groupName = sg
                icon = self.gIcons.icon(groupName)

                if sg in currG:
                    currG = currG[sg]
                    if currG["title"] in currT :
                        currT = currT[currG["title"]]
                        parentItem = currT["item"]
                    else :
                        # create the item
                        item = None
                        if parentItem:
                            item = yui.YTreeItem(parentItem, currG["title"], icon)
                        else :
                            item = yui.YTreeItem(currG["title"], icon)
                        item.this.own(False)
                        currT[currG["title"]] = { "item" : item, "name" : groupName }
                        currT = currT[currG["title"]]
                        parentItem = item
                else:
                    # group is not in our group definition, but it's into the repository
                    # we just use it
                    if sg in currT :
                        currT = currT[sg]
                        parentItem = currT["item"]
                    else :
                        item = None
                        if parentItem:
                            item = yui.YTreeItem(parentItem, sg, icon)
                        else :
                            item = yui.YTreeItem(sg, icon)
                        item.this.own(False)
                        currT[sg] = { "item" : item, "name": groupName }
                        currT = currT[sg]
                        parentItem = item

        print ("End found %d groups" %len(rpm_groups))

        keylist = sorted(self.groupList.keys())
        v = []
        for key in keylist :
            item = self.groupList[key]['item']
            v.append(item)

        itemCollection = yui.YItemCollection(v)
        self.tree.startMultipleChanges()
        self.tree.deleteAllItems()
        self.tree.doneMultipleChanges()
        self.tree.addItems(itemCollection)
        yui.YUI.app().normalCursor()


    def _formatLink(self, description, url) :
        '''
        @param description: Description to be shown as link
        @param url: to be reach when click on $description link
        returns href string to be published
        '''
        webref = '<a href="%s">%s</a>'%(url, description)

        return webref

    def _setInfoOnWidget(self, pkg) :
        """
        writes package description into info widget
        """
        self.info.setValue("")
        if pkg :
            missing = _("Missing information")
            description = pkg.description.replace("\n", "<br>")
            s = "<h2> %s - %s </h2>%s" %(pkg.name, pkg.summary, description)
            s += "<br>"
            if pkg.is_update :
                s+= '<b>%s</b>'%self._formatLink(self.infoshown['updateinfo']['title'], 'updateinfo')
                s += "<br>"
                if self.infoshown['updateinfo']["show"]:
                    # [{'references': [], 'filenames': [], 'id': 'xxxx', 'title': 'yyyy',  'description': 'desc', 'updated': 'date', 'type': 2}]
                    if pkg.updateinfo :
                        s += '<b>%s</b>'%pkg.updateinfo[0]['title']
                        s += "<br>"
                        s += pkg.updateinfo[0]['description'].replace("\n", "<br>")
                        s += "<br>"
                        s += '<b>%s</b> %s'%(pkg.updateinfo[0]['id'], pkg.updateinfo[0]['updated'].replace("\n", "<br>"))
                    else :
                        s+= missing

            if pkg.repository:
                s += "<br>"
                s += '<b> %s: %s</b>'%(_("Repository"), pkg.repository)
                s += "<br>"

            if pkg.URL:
                s += "<br>"
                s += '<b><a href="%s">%s</a></b>'%(pkg.URL, pkg.URL)
                s += "<br>"

            t = 'requirements'
            s += "<br>"
            s += '<b>%s</b>'%self._formatLink(self.infoshown[t]['title'], t)
            s += "<br>"
            if self.infoshown[t]["show"]:
                if pkg.requirements :
                    s+= "<br>".join(pkg.requirements)
                else:
                    s+= missing

            t = 'files'
            s += "<br>"
            s += '<b>%s</b>'%self._formatLink(self.infoshown[t]['title'], t)
            s += "<br>"
            if self.infoshown[t]["show"]:
                if pkg.filelist :
                    s+= "<br>".join(pkg.filelist)
                else:
                    s+= missing

            t = 'changelog'
            s += "<br>"
            s += '<b>%s</b>'%self._formatLink(self.infoshown[t]['title'], t)
            s += "<br>"
            if self.infoshown[t]["show"]:
                if pkg.changelog:
                    s+= "<br>".join(pkg.changelog)
                else:
                    s+= missing
            self.info.setValue(s)
        else:
          logger.warning("_setInfoOnWidget without package")

    def _showErrorAndContinue(self, title, error):
      '''
      Shows an error dialog box and continue to work, it supposes error is not critical
      (i.e. a wrong search for instance)
      '''
      dialogs.warningMsgBox({'title' : title, "text": error, "richtext":True})
      self._enableAction(True)

    def _showSearchResult(self, packages, createTreeItem=False):
      '''
      Shows search result package list on package view
      if createTreeItem is True clears the table and rebuilds item list
      '''
      sel_pkg = self._selectedPackage()

      #clean up tree
      if createTreeItem:
          self._fillGroupTree()

      filter = self._filterNameSelected()
      self.itemList = {}
      # {
      #   name-epoch_version-release.arch : { pkg: dnf-pkg, item: YItem}
      # }

      # Package API doc: http://dnf.readthedocs.org/en/latest/api_package.html
      for pkg in packages:
        if (filter == 'all' or (filter == 'to_update' and pkg.is_update ) or (filter == 'installed' and pkg.installed) or
            (filter == 'not_installed' and not pkg.installed) or
            (filter == 'skip_other' and (pkg.arch == 'noarch' or pkg.arch == platform.machine()))) :
            item = yui.YCBTableItem(pkg.name , pkg.summary , pkg.version, pkg.release, pkg.arch, pkg.sizeM)
            pkg_name = pkg.fullname
            if sel_pkg :
                if sel_pkg.fullname == pkg_name :
                    item.setSelected(True)
            item.check(self.packageQueue.checked(pkg))
            self.itemList[pkg_name] = {
                'pkg' : pkg, 'item' : item
                }
            if not self.update_only:
                item.addCell(" ")
                self._setStatusToItem(pkg,item)
            item.this.own(False)

      keylist = sorted(self.itemList.keys())
      v = []
      for key in keylist :
          item = self.itemList[key]['item']
          v.append(item)

      itemCollection = yui.YItemCollection(v)

      self.packageList.startMultipleChanges()
      # cleanup old changed items since we are removing all of them
      self.packageList.setChangedItem(None)
      self.packageList.deleteAllItems()
      self.packageList.addItems(itemCollection)
      self.packageList.doneMultipleChanges()

      if createTreeItem:
          self.tree.startMultipleChanges()
          icon = self.gIcons.icon("Search")
          treeItem = yui.YTreeItem(self.gIcons.groups['Search']['title'] , icon, False)
          treeItem.setSelected(True)
          self.groupList[self.gIcons.groups['Search']['title']] = { "item" : treeItem, "name" : "Search" }
          self.tree.addItem(treeItem)
          self.tree.doneMultipleChanges()
          self.tree.rebuildTree()

      self._enableAction(True)


    def _searchPackages(self) :
        '''
        retrieves the info from search input field and from the search type list
        to perform a package research and to fill the package list widget

        return False if an empty string used
        '''

        type_item = self.search_list.selectedItem()
        #fields = []
        #for field in self.local_search_types.keys():
            #if self.local_search_types[field]['item'] == type_item:
                #fields.append(field)
                #break
        fields = [ field for field in self.local_search_types.keys() if self.local_search_types[field]['item'] == type_item ]
        field = fields[0] if fields else None

        if field == 'name' or field == 'summary':
          self.use_regexp.setEnabled()
        else:
          self.use_regexp.setChecked(False)
          self.use_regexp.setEnabled(False)

        search_string = self.find_entry.value()
        if not search_string :
          return False

        if self.use_regexp.isEnabled() and self.use_regexp.isChecked() :
          # fixing attribute names
          if field == 'name' :
            field = 'fullname'
          filters = {
            'installed'     : 'installed',
            'not_installed' : 'available',
            'to_update'     : 'updates',
            'all'           : 'all',
            'skip_other'    : 'all',
          }
          filter = filters[self._filterNameSelected()]
          self.backend.search(filter, field, search_string)
        else:
          strings = re.split('[ ,|:;]',search_string)
          ### TODO manage tags
          tags =""
          attrs = ['summary', 'size', 'group', 'action']
          self.backend.Search(fields, strings, attrs, self.match_all, self.newest_only, tags)

        self._enableAction(False)

        return True

    def _populate_transaction(self) :
        '''
        Clear and populate a transaction
        '''
        sync = True
        self.backend.ClearTransaction(sync)
        for action in const.QUEUE_PACKAGE_TYPES:
            pkg_ids = self.packageQueue.get(action)
            for pkg_id in pkg_ids:
                logger.debug('adding: %s %s' %(const.QUEUE_PACKAGE_TYPES[action], pkg_id))
                rc, trans = self.backend.AddTransaction(
                    pkg_id, const.QUEUE_PACKAGE_TYPES[action], sync)
                if not rc:
                    logger.error('AddTransaction result : %s: %s' % (rc, pkg_id))

    def _undo_transaction(self):
        '''
        Run the undo transaction
        '''
        locked = False
        performedUndo = False
        sync = True

        try:
            rc, result = self.backend.GetTransaction(sync)
            if rc :
                transaction_result_dlg = dialogs.TransactionResult(self)
                ok = transaction_result_dlg.run(result)

                if ok:  # Ok pressed
                    self.infobar.info(_('Undo transaction'))
                    rc, result = self.backend.RunTransaction(sync)
                    # This can happen more than once (more gpg keys to be
                    # imported)
                    while rc == 1:
                        logger.debug('GPG key missing: %s' % repr(result))
                        # get info about gpgkey to be confirmed
                        values = self.backend._gpg_confirm
                        if values:  # There is a gpgkey to be verified
                            (pkg_id, userid, hexkeyid, keyurl, timestamp) = values
                            logger.debug('GPGKey : %s' % repr(values))
                            resp = dialogs.ask_for_gpg_import(values)
                            self.backend.ConfirmGPGImport(hexkeyid, resp, sync)
                            # tell the backend that the gpg key is confirmed
                            # rerun the transaction
                            # FIXME: It should not be needed to populate
                            # the transaction again
                            if resp:
                                rc, result = self.backend.GetTransaction(sync)
                                rc, result = self.backend.RunTransaction(sync)
                            else:
                                # NOTE TODO answer no is the only way to exit, since it seems not
                                # to install the key :(
                                break
                        else:  # error in signature verification
                            dialogs.infoMsgBox({'title' : _('Error checking package signatures'),
                                                'text' : '<br>'.join(result), 'richtext' : True })
                            break
                    if rc == 4:  # Download errors
                        dialogs.infoMsgBox({'title'  : ngettext('Downloading error',
                            'Downloading errors', len(result)), 'text' : '<br>'.join(result), 'richtext' : True })
                        logger.error('Download error')
                        logger.error(result)
                    elif rc != 0:  # other transaction errors
                        dialogs.infoMsgBox({'title'  : ngettext('Error in transaction',
                                    'Errors in transaction', len(result)), 'text' :  '<br>'.join(result), 'richtext' : True })
                        logger.error('RunTransaction failure')
                        logger.error(result)

                    self.release_root_backend()
                    self.backend.reload()
                    performedUndo = (rc == 0)
            else:
                logger.error('BuildTransaction failure')
                logger.error(result)
                s = "%s"%result
                dialogs.warningMsgBox({'title' : _("Build transaction failure"), "text": s, "richtext":True})
        except dnfdaemon.client.AccessDeniedError as e:
            logger.error("dnfdaemon client AccessDeniedError: %s ", e)
            dialogs.warningMsgBox({'title' : _("Build transaction failure"), "text": _("dnfdaemon client not authorized:%(NL)s%(error)s")%{'NL': "\n",'error' : str(e)}})
        except:
            exc, msg = misc.parse_dbus_error()
            if 'AccessDeniedError' in exc:
                logger.warning("User pressed cancel button in policykit window")
                logger.warning("dnfdaemon client AccessDeniedError: %s ", msg)
            else:
                pass

        return performedUndo


    def saveUserPreference(self):
        '''
        Save user preferences on exit and view layout if needed
        '''
        filter = self._filterNameSelected()
        view = self._viewNameSelected()
        self.config.userPreferences['view'] = {
            'show': view,
            'filter': filter
            }

        search = {
            'match_all': self.match_all,
            'newest_only': self.newest_only
          }

        if 'settings' in self.config.userPreferences.keys() :
            settings = self.config.userPreferences['settings']
            settings['search'] = search
            if 'show updates at startup' in settings.keys() :
                if settings['show updates at startup'] :
                    self.config.userPreferences['view']['filter'] = 'to_update'
            if 'do not show groups at startup' in settings.keys():
                if settings['do not show groups at startup'] :
                    self.config.userPreferences['view']['show'] = 'all'
        self.config.saveUserPreferences()

    def _load_history(self, transactions):
        '''
        Load history and populate view.
        Args:
            list of (transaction is, date-time) pairs
        '''
        hw = dialogs.HistoryView(self)
        undo = hw.run(transactions)
        hw = None
        return undo

    def handleevent(self):
        """
        Event-handler for the maindialog
        """
        self.running = True
        while self.running == True:
            event = self.dialog.waitForEvent(200)

            eventType = event.eventType()

            rebuild_package_list = False
            group = None
            #event type checking
            if (eventType == yui.YEvent.CancelEvent) :
                self.running = False
                break
            elif (eventType == yui.YEvent.MenuEvent) :
                ### MENU ###
                item = event.item()
                if (item) :
                    if  item == self.fileMenu['reset_sel'] :
                        self.packageQueue.clear()
                        rebuild_package_list = self._rebuildPackageListWithSearchGroup()
                    elif item == self.fileMenu['reload'] :
                        self.infobar.reset_all()
                        self.infobar.info(_('Refreshing Repository Metadata'))
                        self.reset_cache()
                        self._enableAction(False)
                        self.pbar_layout.setEnabled(True)
                    elif item == self.fileMenu['repos']:
                        rd = dialogs.RepoDialog(self)
                        rd.run()
                    elif item == self.fileMenu['quit'] :
                        #### QUIT
                        self.running = False
                        break
                    elif item == self.optionsMenu['user_prefs'] :
                        up = dialogs.OptionDialog(self)
                        up.run()
                    elif item == self.infoMenu['history'] :
                        self.backend.GetHistoryByDays(0, 120) #TODO add in config file
                    elif item == self.helpMenu['help']  :
                        info = helpinfo.DNFDragoraHelpInfo()
                        hd = helpdialog.HelpDialog(info)
                        hd.run()
                    elif item == self.helpMenu['about']  :
                        self.AboutDialog.run()
                else:
                    url = yui.toYMenuEvent(event).id()
                    if url :
                        logger.debug("Url selected: %s", url)
                        if url in self.infoshown.keys():
                            self.infoshown[url]["show"] = not self.infoshown[url]["show"]
                            logger.debug("Info to show: %s"%(self.infoshown))
                            sel_pkg = self._selectedPackage()
                            self._setInfoOnWidget(sel_pkg)
                            self._selPkg = sel_pkg
                        else :
                            logger.debug("run browser, URL: %s"%url)
                            webbrowser.open(url, 2)
            elif (eventType == yui.YEvent.WidgetEvent) :
                # widget selected
                widget  = event.widget()
                if (widget == self.quitButton) :
                    #### QUIT
                    self.running = False
                    break
                elif (widget == self.packageList) :
                    wEvent = yui.toYWidgetEvent(event)
                    if (wEvent.reason() == yui.YEvent.ValueChanged) :
                        changedItem = self.packageList.changedItem()
                        if changedItem :
                            for it in self.itemList:
                                if (self.itemList[it]['item'] == changedItem) :
                                    pkg = self.itemList[it]['pkg']
                                    if pkg.installed and self.backend.protected(pkg) :
                                        dialogs.warningMsgBox({'title' : _("Protected package selected"), "text": _("Package %s cannot be removed")%pkg.name, "richtext":True})
                                        rebuild_package_list = self._rebuildPackageListWithSearchGroup()
                                    else :
                                        if changedItem.checked():
                                            self.packageQueue.add(pkg, 'i')
                                        else:
                                            self.packageQueue.add(pkg, 'r')
                                        self._setStatusToItem(pkg, self.itemList[it]['item'], True)
                                    break
                    else:
                      logger.debug("package list selected, but no items changed")

                elif (widget == self.reset_search_button) :
                    #### RESET
                    rebuild_package_list = True
                    self.find_entry.setValue("")
                    self._fillGroupTree()

                elif (widget == self.find_button) or (widget == self.search_list) or (widget == self.use_regexp):
                    #### FIND
                    if not self._searchPackages() :
                      rebuild_package_list = True
                      self._fillGroupTree()

                elif (widget == self.checkAllButton) :
                    for it in self.itemList:
                        pkg = self.itemList[it]['pkg']
                        self.packageQueue.add(pkg, 'i')
                    rebuild_package_list = self._rebuildPackageListWithSearchGroup()

                elif (widget == self.applyButton) :
                    #### APPLY
                    # disable actions
                    self._enableAction(False)
                    self.pbar_layout.setEnabled(True)
                    # for some reasons here does not refresh the layout, let's force it with a poll request
                    self.dialog.pollEvent()

                    self._populate_transaction()
                    self.backend.BuildTransaction()

                elif (widget == self.view_box) :
                    view = self._viewNameSelected()
                    filter = self._filterNameSelected()
                    self.checkAllButton.setEnabled(filter == 'to_update')
                    rebuild_package_list = True
                    #reset find entry, it does not make sense here
                    self.find_entry.setValue("")
                    self._fillGroupTree()

                elif (widget == self.tree):
                  rebuild_package_list = True
                  self.find_entry.setValue("")

                elif (widget == self.filter_box) :
                  view = self._viewNameSelected()
                  filter = self._filterNameSelected()
                  self._fillGroupTree()
                  self.checkAllButton.setEnabled(filter == 'to_update')
                  rebuild_package_list = not self._searchPackages()
                else:
                    print(_("Unmanaged widget"))
            elif (eventType == yui.YEvent.TimeoutEvent) :
              rebuild_package_list = self._manageDnfDaemonEvent()
              if not self.backend_locked :
                logger.debug("Status: %s", self._status)
                if self._status != DNFDragoraStatus.LOCKING:
                  self._beforeLockAgain -= 1
                  if self._beforeLockAgain == 1:
                    logger.debug("Try locking again")
                    self._status = DNFDragoraStatus.LOCKING
                    self._beforeLockAgain = 20
                    self.backend.Lock()
                    rebuild_package_list = False
              if rebuild_package_list:
                logger.debug("Rebuilding %s", rebuild_package_list)
                self._fillGroupTree()

            else:
                print(_("Unmanaged event %d"), eventType)

            if rebuild_package_list :
              filter = self._filterNameSelected()
              search_string = self.find_entry.value()
              if not search_string :
                sel = self.tree.selectedItem()
                if sel :
                  group = self._groupNameFromItem(self.groupList, sel)
                  self._fillPackageList(group, filter)

            sel_pkg = self._selectedPackage()
            if sel_pkg :
              if self._selPkg != sel_pkg:
                self._setInfoOnWidget(sel_pkg)
                self._selPkg = sel_pkg
            else:
              self.info.setValue("")

            if self.packageQueue.total() > 0 and not self.applyButton.isEnabled():
                self.applyButton.setEnabled()
            elif self.packageQueue.total() == 0 and self.applyButton.isEnabled():
                self.applyButton.setEnabled(False)

        # Save user prefs on exit
        self.saveUserPreference()

        if yui.YUI.app().isTextMode():
          self.glib_loop.quit()

        self.dialog.destroy()

        try:
            self.backend.quit()
        except Exception as err:
            logger.error("Excpion on exit %s", err)
        self.loop_has_finished = True

        self.backend.waitForLastAsyncRequestTermination()

        if yui.YUI.app().isTextMode():
          self.glib_thread.join()

        #self.backend.quit()
        #self.backend.release_root_backend()

    def _OnRepoMetaDataProgress(self, name, frac):
      '''Repository Metadata Download progress.'''
      values = (name, frac)
      #print('on_RepoMetaDataProgress (root): %s', repr(values))
      logger.debug('OnRepoMetaDataProgress: %s', repr(values))
      if frac == 0.0:
        self.infobar.reset_all()
        self.infobar.info_sub(name)
      elif frac == 1.0:
        self.infobar.set_progress(1.0)
      else:
        self.infobar.set_progress(frac)

    def _OnTransactionEvent(self, event, data):
      ''' Manage a transaction event'''
      values = (event, data)
      logger.debug('OnTransactionEvent: %s', repr(values))

      if event == 'start-run':
        self.infobar.info(_('Start transaction'))
      elif event == 'download':
          self.infobar.info(_('Downloading packages'))
      elif event == 'pkg-to-download':
        #TODO manage event pkg-to-download
        self._dnl_packages = data
      elif event == 'signature-check':
        # self.infobar.show_progress(False)
        self.infobar.set_progress(0.0)
        self.infobar.info(_('Checking package signatures'))
        self.infobar.set_progress(1.0)
        self.infobar.info_sub('')
      elif event == 'run-test-transaction':
        # self.infobar.info(_('Testing Package Transactions')) #
        # User don't care
        pass
      elif event == 'run-transaction':
        self.infobar.info(_('Applying changes to the system'))
        self.infobar.info_sub('')
      elif event == 'verify':
        self.infobar.info(_('Verify changes on the system'))
        #self.infobar.hide_sublabel()
      # elif event == '':
      elif event == 'fail':
        logger.error('TransactionEvent failure: %s', repr(values))
        self.infobar.reset_all()
      elif event == 'end-run':
        self.infobar.set_progress(1.0)
        self.infobar.reset_all()
      elif event == 'start-build':
        self.infobar.set_progress(0.0)
        self.infobar.info(_('Build transaction'))
      elif event == 'end-build':
        self.infobar.set_progress(1.0)
      else:
        logger.error('Unmanaged transaction event : %s', str(event))

    def _OnRPMProgress(self, package, action, te_current, te_total, ts_current, ts_total):
      ''' _OnRPMProgress manages RPM Progress event
          Parameters
            @param package:    package name
            @param action:     constant transaction set state (which ones?)
            @param te_current: current number of bytes processed in the transaction element being processed
            @param te_total:   total number of bytes in the transaction element being processed
            @param ts_current: number of processes completed in whole transaction
            @param ts_total:   total number of processes in the transaction.
      '''
      values = (package, action, te_current, te_total, ts_current, ts_total)
      if (te_current == 0 or te_current == te_total):
        logger.debug('OnRPMProgress: %s', repr(values))

      num = ' ( %i/%i )' % (ts_current, ts_total)
      if ',' in package:  # this is a pkg_id
        name = dnfdragora.misc.pkg_id_to_full_name(package)
      else:  # this is just a pkg name (cleanup)
        name = package

      if (not self._package_name or name != self._package_name or self._action_name != action):
        #let's log once
        logger.debug('OnRPMProgress : [%s]', package)
        self._package_name = name
        self._action_name = action
        try:
          self.infobar.info_sub(const.RPM_ACTIONS[action] % name)
        except KeyError:
          logger.error('OnRPMProgress: unknown action %s', action)
          self.infobar.info_sub(_("Unknown action %(action)s on %(name)s")%({'action':action, 'name':name}))

      if te_current > 0 and te_current <= te_total:
          frac = float(te_current) / float(te_total)
          self.infobar.set_progress(frac, label=num)

    def _OnGPGImport(self, pkg_id, userid, hexkeyid, keyurl, timestamp):
        values = (pkg_id, userid, hexkeyid, keyurl, timestamp)
        self._gpg_confirm = values
        logger.debug('OnGPGImport %s', repr(values))
        # get info about gpgkey to be comfirmed
        if values:  # There is a gpgkey to be verified
            logger.debug('GPGKey : %s' % repr(values))
            resp = dialogs.ask_for_gpg_import(values)
            self.backend.ConfirmGPGImport(hexkeyid, resp, sync=True)

    def _OnDownloadStart(self, num_files, num_bytes):
      '''Starting a new parallel download batch.'''
      values =  (num_files, num_bytes)
      logger.debug('OnDownloadStart %s', repr(values))

      self._files_to_download = num_files
      self._files_downloaded = 1
      self.infobar.set_progress(0.0)
      self.infobar.info_sub(
          _('Downloading %(count_files)d files (%(count_bytes)sB)...') %
          {'count_files':num_files, 'count_bytes': dnfdragora.misc.format_number(num_bytes)})

    def _OnDownloadProgress(self, name, frac, total_frac, total_files):
      '''Progress for a single element in the batch.'''
      values =  (name, frac, total_frac, total_files)
      if total_frac == 1.0:
        logger.debug('OnDownloadProgress %s', repr(values))

      num = '( %d/%d - %s)' % (self._files_downloaded, self._files_to_download, name)
      self.infobar.set_progress(total_frac, label=num)

    def _OnDownloadEnd(self, name, status, msg):
      '''Download of af single element ended.'''
      values =  (name, status, msg)
      logger.debug('OnDownloadEnd %s', repr(values))

      if status == -1 or status == 2:  # download OK or already exists
        logger.debug('Downloaded : %s', name)
        self._files_downloaded += 1
      else:
        logger.error('Download Error : %s - %s', name, msg)
      #self.infobar.set_progress(1.0)
      #self.infobar.reset_all()

    def _OnErrorMessage(self,  msg):
      logger.error('OnErrorMessage %s', str(msg))

    def _cachingRequest(self, pkg_flt):
      '''
      request for packages to be cached
      @params pkg_flt (available, installed, updates)
      '''
      logger.debug('Start caching %s', pkg_flt)
      fields = ['summary', 'size', 'group']  # fields to get
      if pkg_flt == 'updates' or pkg_flt == 'updates_all':
        self.infobar.info_sub(_("Caching updates"))
        self._status = DNFDragoraStatus.CACHING_UPDATE
      elif pkg_flt == 'installed':
        self.infobar.info_sub(_("Caching installed"))
        self._status = DNFDragoraStatus.CACHING_INSTALLED
      elif pkg_flt == 'available':
        self.infobar.info_sub(_("Caching available"))
        self._status = DNFDragoraStatus.CACHING_AVAILABLE
      else:
        logger.error("Wrong package filter %s", pkg_flt)
        return
      self.backend.GetPackages(pkg_flt, fields)


    def _populateCache(self, pkg_flt, po_list) :
      if pkg_flt == 'updates_all':
        pkg_flt = 'updates'
      # is this type of packages is already cached ?
      if not self.backend.cache.is_populated(pkg_flt):
        pkgs = self.backend.make_pkg_object(po_list, pkg_flt)
        self.backend.cache.populate(pkg_flt, pkgs)

    def _check_MD_cache_expired(self):
      ''' Check metadata expired '''
      # check if MD cache management is disabled
      if self.md_update_interval <= 0:
        return False
      # check this is the first time dnfdragora is run for this user
      if not self.md_last_refresh_date:
        return True

      time_fmt = '%Y-%m-%d %H:%M'
      now = datetime.datetime.now()
      refresh_period = datetime.timedelta(hours=self.md_update_interval)
      last_refresh = datetime.datetime.strptime(self.md_last_refresh_date, time_fmt)
      period = now - last_refresh
      return period > refresh_period

    def _set_MD_cache_refreshed(self):
      ''' set  '''
      time_fmt = '%Y-%m-%d %H:%M'
      now = datetime.datetime.now()
      now_str = now.strftime(time_fmt)
      self.config.userPreferences['settings']['metadata']['last_update'] = now_str
      self.md_last_refresh_date =  now_str

    def _start_caching_packages(self):
      ''' Start caching packages from installed
        next ones are requested after automatically.
      '''
      self.infobar.reset_all()
      self.backend.cache.reset()
      self.infobar.info(_('Creating packages cache'))
      self._cachingRequest('installed')

    def _OnBuildTransaction(self, info):
      ''' manages BuildTransaction event from dnfdaemon '''
      self.infobar.reset_all()
      if not info['error']:
        ok, result = info['result']
        # If status is RUN_TRANSACTION we have already confirmed our transaction into BuildTransaction
        # and we are here most probably for a GPG key confirmed during last transaction
        if ok and not self.always_yes and self._status != DNFDragoraStatus.RUN_TRANSACTION:
          transaction_result_dlg = dialogs.TransactionResult(self)
          ok = transaction_result_dlg.run(result)
          if not ok:
            self._enableAction(True)
            return

        if ok:
          self.infobar.info(_('Applying changes to the system'))
          self.backend.RunTransaction()
          self._status = DNFDragoraStatus.RUN_TRANSACTION
        else:
          err =  "".join(result) if isinstance(result, list) else result if isinstance(result, list) else repr(result);
          dialogs.infoMsgBox({'title'  : _('Build Transaction error',), 'text' : err.replace("\n", "<br>"), 'richtext' : True })
          logger.warning("Transaction Cancelled: %s", repr(result))
          self._enableAction(True)

    def _OnRunTransaction(self, info):
      ''' manages RunTransaction event from dnfdaemon '''
      rc, result = info['result']
      if rc == 1:
        logger.warning('GPG key missing: %s' % repr(result))
        # NOTE should have been managed into OnGPGImport, so we can run transaction again
        self._transaction_tries += 1
        if self._transaction_tries <= 3:
          self._populate_transaction()
          self.backend.BuildTransaction()
          return
        else  :
          dialogs.warningMsgBox({'title' :_("GPG key missing"), 'text' : repr(result), 'richtext' : True})
      elif rc == 4:
        dialogs.infoMsgBox({'title'  : ngettext('Downloading error',
                            'Downloading errors', len(result)), 'text' : '<br>'.join(result), 'richtext' : True })
        logger.error('Download error')
        logger.error(result)
      elif rc != 0:  # other transaction errors
        dialogs.infoMsgBox({'title'  : ngettext('Error in transaction',
                            'Errors in transaction', len(result)), 'text' :  '<br>'.join(result), 'richtext' : True })
        logger.error('RunTransaction failure')
        logger.error(result)

      self._transaction_tries = 0
      self.backend.Unlock(sync=True)
      self.backend.clear_cache()
      self.packageQueue.clear()
      self._status = DNFDragoraStatus.STARTUP
      self.backend.Lock()

    def _manageDnfDaemonEvent(self):
      '''
      get events from dnfd client queue
      '''
      rebuild_package_list = False
      try:
        counter = 0
        # On RUNNING we manage UI events, on other status dnfdaeomon let's try to increase
        # dequeuing when we're not in RUNNING then
        count_max = 1000 if self._status != DNFDragoraStatus.RUNNING else 1

        while counter < count_max:
          counter = counter + 1
          item = self.backend.eventQueue.get_nowait()
          event = item['event']
          info = item['value']
          if self._status != DNFDragoraStatus.RUN_TRANSACTION:
            logger.debug("Event received %s - status %s", event, self._status)

          is_dict = isinstance(info, dict)
          if is_dict:
            if 'error' in info.keys() and 'result' in info.keys():
              if info['error']:
                logger.error("Event received %s, %s - status %s", event, info['error'], self._status)
              elif info['result']:
                is_list = isinstance(info['result'], list)
                logger.debug("Event received %s, %s - status %s", event, len(info['result']) if is_list else info, self._status)

          if (event == 'Lock') :
            self.backend_locked = info['result']
            logger.info("Event %s received (%s)", event, info['result'])
            if self.backend_locked :
              self._status = DNFDragoraStatus.RUNNING
              self.backend.SetWatchdogState(False, sync=True)
              # TODO only if expired
              if self._check_MD_cache_expired():
                self.backend.ExpireCache()
              else:
                self._start_caching_packages()
            else:
              self._enableAction(self.backend_locked)
              if self._status == DNFDragoraStatus.LOCKING:
                if not info['error']:
                  self._status = DNFDragoraStatus.STARTUP
          elif (event == 'Unlock') :
            logger.info("Event %s received (%s)", event, info['result'])
            self.backend_locked = False
          elif (event == 'ExpireCache'):
            # ExpireCache has been invoked let's refresh the date
            self._set_MD_cache_refreshed()
            # After metadata refresh let's build package cache
            self._start_caching_packages()
          elif (event == 'GetPackages'):
            if not info['error']:
              if self._status == DNFDragoraStatus.CACHING_INSTALLED:
                po_list = info['result']
                # we requested installed for caching
                self._populateCache('installed', po_list)
                self.infobar.set_progress(0.33)
                cache_update = 'updates_all' if self.upgrades_as_updates else 'updates'
                self._cachingRequest(cache_update)
              elif self._status == DNFDragoraStatus.CACHING_UPDATE:
                po_list = info['result']
                # we requested updates for caching
                self._populateCache('updates', po_list)
                self.infobar.set_progress(0.66)
                self._cachingRequest('available')
              elif self._status == DNFDragoraStatus.CACHING_AVAILABLE:
                po_list = info['result']
                rpm_groups = None
                if self.use_comps :
                  # let's show the dialog with a poll event
                  rpm_groups = self.backend.GetGroups(sync=True)
                self.gIcons = compsicons.CompsIcons(rpm_groups, self.group_icon_path) if self.use_comps else  groupicons.GroupIcons(self.group_icon_path)

                # we requested available for caching
                self.infobar.set_progress(1.0)
                self._populateCache('available', po_list)
                self._status = DNFDragoraStatus.RUNNING

                if not self._runtime_option_managed and 'install' in self.options.keys() :
                  pkgs = " ".join(self.options['install'])
                  self.backend.Install(pkgs, sync=True)
                  self.backend.BuildTransaction()
                  self._runtime_option_managed = True
                  return

                self._enableAction(self.backend_locked)
                filter = self._filterNameSelected()
                self.checkAllButton.setEnabled(filter == 'to_update')

                sel = self.tree.selectedItem()
                if sel :
                  rebuild_package_list = self._rebuildPackageListWithSearchGroup()
                else:
                  rebuild_package_list = True
                self.infobar.reset_all()
            else:
              logger.error("GetPackages error: %s", info['error'])
              raise UIError(str(info['error']))

          elif (event == 'RESearch'):
            if not info['error']:
              packages = info['result']
              self._showSearchResult(packages, createTreeItem=True)
            else:
              self._showErrorAndContinue(_("Search error using regular expression"), info['error'])
              logger.error("Search error: %s", info['error'])
          elif (event == 'Search'):
            if not info['error']:
              packages = self.backend.make_pkg_object_with_attr(info['result'])
              self._showSearchResult(packages, createTreeItem=True)
            else:
              logger.error("Search error: %s", info['error'])
              raise UIError(str(info['error']))

          elif (event == 'OnRepoMetaDataProgress'):
            self._OnRepoMetaDataProgress(info['name'], info['frac'])
          elif (event == 'BuildTransaction'):
            self._OnBuildTransaction(info)
          elif (event == 'RunTransaction'):
            if not info['error']:
              self._OnRunTransaction(info)
            else:
              res = info['error']
              if isinstance(res, Exception) :
                if 'AccessDeniedError' in str(res):
                  # user should have pressed cancel on auth. Wrong password alert seems to be managed outside too
                  # avoid a dialog to recall the user what he did, let's see if we have negative feedbacks
                  # dialogs.warningMsgBox({'title' : _("AccessDeniedError"), "text": _("Cancel or wrong password given:%(NL)s%(error)s")%{'NL': "\n",'error' : str(res)}})

                  # we've already confirmed build transaction but failed authentication if for any reasons we reset or change the selection
                  # next transaction is confirmed without asking so we change the status and ask for confirmation again
                  self._status = DNFDragoraStatus.RUNNING
                  self._enableAction(True)
              else:
                logger.error("RunTransaction error: %s", str(info['error']))
                raise UIError(str(info['error']))
          elif (event == 'OnTransactionEvent'):
            self._OnTransactionEvent(info['event'], info['data'])
          elif (event == 'OnRPMProgress'):
            self._OnRPMProgress(info['package'], info['action'], info['te_current'],
                                info['te_total'], info['ts_current'], info['ts_total'])
          elif (event == 'OnGPGImport'):
            self._OnGPGImport(info['pkg_id'], info['userid'], info['hexkeyid'],
                              info['keyurl'], info['timestamp'])
          elif (event == 'OnDownloadStart'):
            self._OnDownloadStart(info['num_files'], info['num_bytes'])
          elif (event == 'OnDownloadProgress'):
            self._OnDownloadProgress(info['name'], info['frac'], info['total_frac'], info['total_files'])
          elif (event == 'OnDownloadEnd'):
            self._OnDownloadEnd(info['name'], info['status'], info['msg'])
          elif (event == 'OnErrorMessage'):
            self._OnErrorMessage(info['msg'])
          elif (event == 'GetHistoryByDays'):
            if not info['error']:
              transaction_list = info['result']
              self._load_history(transaction_list)
              #### TODO fix history undo transaction
              # TODO if performedUNDO:
              # TODO   sel = self.tree.selectedItem()
              # TODO   if sel :
              # TODO       group = self._groupNameFromItem(self.groupList, sel)
              # TODO       filter = self._filterNameSelected()
              # TODO       if (group == "Search"):
              # TODO           # force tree rebuilding to show new package status
              # TODO           if not self._searchPackages(filter, True) :
              # TODO               rebuild_package_list = True
              # TODO       else:
              # TODO           if filter == "to_update":
              # TODO               self._fillGroupTree()
              # TODO           rebuild_package_list = True
          elif (event == 'HistoryUndo'):
            self._undo_transaction()
          elif (event == 'SetEnabledRepos'):
            self.backend.Unlock(sync=True)
            # Enabled repositories are changes we need to force caching again
            self.backend.clear_cache(also_groups=True)
            self.backend.Lock()

          else:
            logger.warning("Unmanaged event received %s - info %s", event, str(info))

      except Empty as e:
        pass

      return rebuild_package_list

    def quit(self):
        self.running = False

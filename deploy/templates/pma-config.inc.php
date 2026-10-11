<?php
/* EdgeShell phpMyAdmin hardening. Installed to /etc/phpmyadmin/conf.d/. */
$i = 1;
$cfg['Servers'][$i]['auth_type']       = 'cookie';
$cfg['Servers'][$i]['host']            = 'localhost';
$cfg['Servers'][$i]['connect_type']    = 'socket';
$cfg['Servers'][$i]['socket']          = '/run/mysqld/mysqld.sock';
$cfg['Servers'][$i]['AllowRoot']       = false;
$cfg['Servers'][$i]['AllowNoPassword'] = false;
$cfg['Servers'][$i]['hide_db']         = '^(information_schema|performance_schema|mysql|sys)$';

$cfg['AllowArbitraryServer']  = false;
$cfg['LoginCookieValidity']   = 1800;   // 30 min idle logout
$cfg['LoginCookieStore']      = 0;      // cookie dies with the browser
$cfg['LoginCookieDeleteAll']  = true;
$cfg['VersionCheck']          = false;
$cfg['SendErrorReports']      = 'never';
$cfg['ShowPhpInfo']           = false;
$cfg['ShowCreateDb']          = false;
$cfg['ShowGitRevision']       = false;
$cfg['NavigationDisplayServers'] = false;
$cfg['TempDir']               = '/var/lib/phpmyadmin/tmp';
$cfg['DefaultLang']           = 'en';

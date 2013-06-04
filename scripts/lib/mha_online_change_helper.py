import time
from datetime import datetime
from mysql_helper import MySQL_helper
from mha_config_helper import MHA_config_helper

class MHA_online_change_helper(object):
    def __init__(self, orig_master_ip, new_master_ip, privileged_users):
        config_helper = MHA_config_helper()
        user = config_helper.get_username()
        password = config_helper.get_password()

	self._orig_master = MySQL_helper(host=orig_master_ip, user=user, password=password)
	self._new_master = MySQL_helper(host=new_master_ip, user=user, password=password)

        self._privileged_users = privileged_users
	self._user_grants_orig_master = []

    def debug_message(self, message):
	current_datetime = datetime.now().strftime("%Y-%m-%-d %H:%M:%S")
	print "[%s] %s" % (current_datetime, message)

    def get_connected_threads(self, master_obj):
    	""" Returns all threads currently connected to MySQL except for the following:
        - The thread corresponding to the connection of this script
        - The thread belonging to the user "system user"
        - The thread doing "Binlog Dump"
        - The thread that has been in sleeping state for more than a second"""

        my_connection_id = master_obj.get_connection_id()
        threads = []

	processlist = master_obj.get_processlist()
	if processlist == False:
	    return False

        for row in processlist:
            connection_id = row['Id']
            user = row['User']
            host = row['Host']
            command = row['Command']
            state = row['State']
            query_time = row['Time']
            info = row['Info']
            if (my_connection_id == connection_id or 
                command == "Binlog Dump" or
                user == "system user" or 
                (command == "Sleep" and query_time >= 1)):
                continue
            threads.append(row)

        return threads

    def revoke_all_user_privileges(self, master_obj):
	users = master_obj.get_all_users()
	
	if users == False or len(users) < 1:
	    return False

	for user in users:
            if (user['User'] in self._privileged_users or 
                user['User'] == master_obj.get_current_user() or 
                user['Repl_slave_priv'] == 'Y' or 
                user['Repl_client_priv'] == 'Y'):
		continue
	    username = "'%s'@'%s'" % (user['User'], user['Host'])
	    user_grants = master_obj.get_user_grants(username)
	    if master_obj.revoke_all_privileges(username) == False:
		return False
	    self.debug_message("Privileges revoked for user %s:" % username)
	    for grant in user_grants:
	    	self._user_grants_orig_master.append(grant)
	    	self.debug_message("\t%s" % grant)
	
	return True

    def regrant_all_user_privileges(self, master_obj):
        users = master_obj.get_all_users()
        
        if users == False or len(users) < 1:
            return False

        for user in users:
            if (user['User'] in self._privileged_users or 
                user['User'] == master_obj.get_current_user() or 
                user['Repl_slave_priv'] == 'Y' or 
                user['Repl_client_priv'] == 'Y'):
                continue
            username = "'%s'@'%s'" % (user['User'], user['Host'])
            self.debug_message("Privileges regranted for user %s:" % username)
            grant_errors = 0
            for grant in master_obj.get_user_grants(username):
                self.debug_message("\t%s" % grant)
                if master_obj.execute_admin_query(grant) == False:
                    self.debug_message("\t\tError please try manually")
                    grant_errors += 1

        if grant_errors > 0:
            return False

        return True

    def execute_stop_command(self):
	# Connect to the new master
	if self._new_master.connect() == False:
	    return False

	# Set read_only=1 on the new master to avoid any data inconsistency
	self.debug_message("Setting read_only=1 on the new master ...")
	self._new_master.set_read_only()
	if self._new_master.is_read_only() == False:
	    return False

	# Disconnect from the new master because we do not want to change anything on it now
	self._new_master.disconnect()

	# Connect to the original master
	if self._orig_master.connect() == False:
	    return False

	# we execute everything below in try..finally because we have to 
        # disconnect and enable log_bin at all cost
	try:
	    # Disable binlogging on the original master
	    if self._orig_master.disable_log_bin() == False:
	        return False

	    # Revoke ALL privileges from the users on original master so that no one can write
	    self.debug_message("Revoking ALL PRIVILEGES of users ...")
	    if self.revoke_all_user_privileges(self._orig_master) == False:
	        return False

	    # Wait upto 5 seconds for all connected threads to disconnect
            self.debug_message("Waiting 5s for all connected threads to disconnect")
	    slept_seconds = 0
	    while slept_seconds < 5:
	        threads = self.get_connected_threads(self._orig_master)
	        if len(threads) > 0:
		    time.sleep(1)
	        else:
		    break

	    # Terminate all threads
	    self.debug_message("Terminating all application threads ...")
	    threads = self.get_connected_threads(self._orig_master)
	    if threads == False:
	        return False

	    for thread in threads:
	        self.debug_message("\tTerminating thread Id => %s, User => %s, Host => %s" % 
                                    (thread['Id'], thread['User'], thread['Host']))
	        self._orig_master.kill_connection(connection_id=thread['Id'])

	    # Setting read_only=1 on the original master
	    self._orig_master.set_read_only()
	    if self._orig_master.is_read_only() == False:
                return False
	finally:
	    # Disconnect from the original master and restore binlogging
	    self._orig_master.enable_log_bin()
	    self._orig_master.disconnect()

	return True

    def rollback_stop_command(self):
	# Connect to the original master
        if self._orig_master.connect() == False:
            return False
	
	rollback_error = 0
	self._orig_master.disable_log_bin()

	# remove the read_only flag from the orignal master
	self.debug_message("Removing the read_only flag from original master")
	if self._orig_master.unset_read_only() == False:
	    self.debug_message("\tError, please try manually")
	    rollback_error += 1

	# if any grants were revoked, we need to regrant them
	self.debug_message("Regranting the privileges that were revoked")
	if len(self._user_grants_orig_master) > 0:
	    for grant in self._user_grants_orig_master:
		self.debug_message("\t%s" % grant)
		if self._orig_master.execute_admin_query(grant) == False:
		    self.debug_message("\t\tError, please try manually")
		    rollback_error += 1

	return_val = True
	if rollback_error > 0:
	    self.debug_message("Rollback FAILED, there were %s errors" % rollback_error)
	    return_val = False
	else:
	    self.debug_message("Rollback completed OK")

	# Disconnect from the original master and restore binlogging
	self._orig_master.enable_log_bin()
	self._orig_master.disconnect()

	return return_val

    def execute_start_command(self):
        # Connect to the new master
        if self._new_master.connect() == False:
            return False

        # Remove the read_only flag
        self.debug_message("Removing the read_only flag from the new master")
        self._new_master.unset_read_only()

        # Regrant the privileges for all the users so that they are recreated
        # on the old master
        self.debug_message("Regranting privileges that were revoked")
        self.regrant_all_user_privileges(self._new_master)

        # Disconnect from the new master
        self._new_master.disconnect()

        return True
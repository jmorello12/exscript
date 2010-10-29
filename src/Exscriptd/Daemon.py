# Copyright (C) 2007-2010 Samuel Abels.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2, as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
import os, logging, logging.handlers
from threading               import Thread
from lxml                    import etree
from Order                   import Order
from Exscript                import Host
from Exscript.util.decorator import bind

# Logfile structure:
# /var/log/exscriptd/mydaemon/access.log
# /var/log/exscriptd/mydaemon/myservice/123/host2.log
# /var/log/exscriptd/mydaemon/myservice/123/host3.log

class _AsyncFunction(Thread):
    def __init__ (self, function, *args, **kwargs):
        Thread.__init__(self)
        self.function = function
        self.args     = args
        self.kwargs   = kwargs

    def run(self):
        self.function(*self.args, **self.kwargs)

class Daemon(object):
    def __init__(self,
                 name,
                 database   = None,
                 processors = None,
                 logdir     = None):
        self.name       = name
        self.db         = database
        self.processors = processors
        self.services   = {}
        self.logdir     = os.path.join(logdir, name)
        self.logger     = logging.getLogger('exscriptd_' + name)
        self.logger.setLevel(logging.INFO)
        if not os.path.isdir(self.logdir):
            os.makedirs(self.logdir)

        # Set up logfile rotation.
        logfile = os.path.join(self.logdir, 'access.log')
        handler = logging.handlers.RotatingFileHandler(logfile,
                                                       maxBytes    = 200000,
                                                       backupCount = 5)

        # Define the log format.
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)

    def log(self, order, message):
        msg = '%s/%s/%s: %s' % (self.name,
                                order.get_service_name(),
                                order.get_id(),
                                message)
        self.logger.info(msg)

    def get_logdir(self):
        return self.logdir

    def add_service(self, name, service):
        self.services[name] = service

    def close_open_orders(self):
        self.logger.info('%s: Closing all open orders.' % self.name)
        self.db.close_open_orders()

    def count_orders(self):
        return self.db.count_orders()

    def get_order_from_id(self, order_id):
        return self.db.get_order(id = order_id)

    def get_order_list(self, offset = 0, limit = 0):
        return self.db.get_orders(offset = offset, limit = limit)

    def set_order_status(self, order, status):
        order.status = status
        self.db.save_order(order)
        self.log(order, 'Status is now "%s"' % status)

    def set_order_status_done(self, order):
        order.close()
        self.set_order_status(order, 'completed')

    def save_order(self, order):
        self.log(order, 'Saving order data.')
        return self.db.save_order(order)

    def save_task(self, order, task):
        return self.db.save_task(order, task)

    def _enter_order(self, service, order):
        # Note: This method is called asynchronously.
        # Store the order in the database.
        self.set_order_status(order, 'saving')
        self.save_order(order)

        self.set_order_status(order, 'enter-start')
        try:
            service.enter(order)
        except Exception, e:
            self.log(order, 'Exception: %s' % e)
            order.close()
            self.set_order_status(order, 'enter-error')
            raise

        # If the service did not enqueue anything, it also
        # has no opportunity to mark the order 'done'. So mark
        # it automatically here.
        service._enter_completed_notify(order)

    def _place_order(self, order):
        self.logger.debug('Placing incoming order.')

        # Store it in the database.
        self.set_order_status(order, 'incoming')

        # Loop the requested service up.
        service = self.services.get(order.service)
        if not service:
            order.close()
            self.set_order_status(order, 'service-not-found')
            return

        # Create the log directory.
        os.makedirs(service.get_logname(order))

        # Notify the service of the new order.
        try:
            accepted = service.check(order)
        except Exception, e:
            self.log(order, 'Exception: %s' % e)
            order.close()
            self.set_order_status(order, 'error')
            raise

        if not accepted:
            order.close()
            self.set_order_status(order, 'rejected')
            return
        self.set_order_status(order, 'accepted')

        # Save the order, including the data that was passed.
        # For performance reasons, use a new thread.
        func = _AsyncFunction(self._enter_order, service, order)
        func.start()

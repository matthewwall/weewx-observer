#!/usr/bin/env python
# Copyright 2017 Matthew Wall, all rights reserved

"""Driver for collecting data from Observer by polling over TCP/IP.

This works only on ip4 networks (no ip6 support).

The driver starts by listening on a specified port.  It then sends a UDP
broadcast.  The station responds to the broadcast by connecting to the port.
The driver then sends a request for data on that connection.  The driver
continues to poll the station using the connection.  If the connection fails,
the process begins again.

Thanks to Dr Bob for testing this driver.

Thanks to ws5020 for publishing the first known implementation, written in
PERL and published 16dec2016:
  http://www.wxforum.net/index.php?topic=30471
"""

# FIXME: get more search and query string samples
# FIXME: get raw data packets so we can do proper decoding

import Queue
import socket
import syslog
import threading
import time

import weewx
import weewx.drivers

DRIVER_NAME = 'Observer'
DRIVER_VERSION = '0.2'

def logmsg(dst, msg):
    syslog.syslog(dst, 'observer: %s: %s' %
                  (threading.currentThread().getName(), msg))

def logdbg(msg):
    logmsg(syslog.LOG_DEBUG, msg)

def loginf(msg):
    logmsg(syslog.LOG_INFO, msg)

def logcrt(msg):
    logmsg(syslog.LOG_CRIT, msg)

def logerr(msg):
    logmsg(syslog.LOG_ERR, msg)


def loader(config_dict, engine):
    return Observer(**config_dict[DRIVER_NAME])

def configurator_loader(config_dict):
    return ObserverConfigurator()

def confeditor_loader():
    return ObserverConfEditor()


def _fmt(data):
    return ' '.join(['%02x' % ord(x) for x in data])


class ObserverConfEditor(weewx.drivers.AbstractConfEditor):
    @property
    def default_stanza(self):
        return """
[Observer]
    # This section is for the Observer weather stations.

    # The driver to use
    driver = user.observer

    # How often to poll the device, in seconds
    poll_interval = 10

    # The IP address on which this driver will listen.  Default is no address,
    # which will listen on any available network interface.
    #host = ""

    # The port on which this driver will listen.
    #port = 6500
"""


class ObserverConfigurator(weewx.drivers.AbstractConfigurator):
    def add_options(self, parser):
        super(ObserverConfigurator, self).add_options(parser)
        parser.add_option("--current", dest="current", action="store_true",
                          help="get the current weather conditions")

    def do_options(self, options, parser, config_dict, prompt):
        station = ObserverDriver(**config_dict[DRIVER_NAME])
        if options.current:
            for packet in station.genLoopPackets():
                print packet
                break


class ObserverDriver(weewx.drivers.AbstractDevice):

    DEFAULT_MAP = {
        'outTemp': 'temperature_out',
        'inTemp': 'temperature_in',
        'outHumidity': 'humidity_out',
        'pressure': 'pressure',
        'windSpeed': 'wind_speed',
        'windDir': 'wind_dir',
        'windGust': 'gust_speed',
        'windGustDir': 'gust_dir',
        'rain': 'rain_delta',
        'radiation': 'solar_radiation'}

    def __init__(self, **stn_dict):
        loginf("driver version is %s" % DRIVER_VERSION)
        self.model = stn_dict.get('model', 'WS1001')
        loginf("model is %s" % self.model)
        self.sensor_map = stn_dict.get(
            'sensor_map', ObserverDriver.DEFAULT_MAP)
        loginf("sensor map is %s" % self.sensor_map)
        host = stn_dict.get('host', '')
        port = int(stn_dict.get('port', Observer.DEFAULT_LISTEN_PORT))
        loginf("driver will listen on %s:%s" % (host, port))
        poll_interval = int(stn_dict.get('poll_interval', 10))
        loginf("poll interval is %s" % poll_interval)
        timeout = int(stn_dict.get('timeout', 15))
        loginf("network timeout is %ss" % timeout)
        self.max_tries = int(stn_dict.get('max_tries', 3))
        self.retry_wait = int(stn_dict.get('retry_wait', 5))
        self._station = Observer(host, port, poll_interval, timeout)
        self._queue = Queue.Queue()
        self._thread = None

    @property
    def hardware_name(self):
        return self.model

    def openPort(self):
        if self._thread is None:
            self._thread = ListenThread(self._station, self._queue)
            self._thread.start()

    def closePort(self):
        if self._thread is not None:
            self._thread.stop_running()
            self._thread.join()
            self._thread = None

    def genLoopPackets(self):
        # loop forever waiting for data from the queue.  when we find something
        # on the queue, decode it into a packet, then yield the packet.
        while True:
            try:
                data = self._queue.get(True, 10)
                logdbg("raw data: %s" % _fmt(data))
                pkt = Observer.decode_data(data)
                logdbg("raw packet: %s" % pkt)
                packet = {'dateTime': int(time.time() + 0.5)}
                for k in self.sensor_map:
                    if self.sensor_map[k] in pkt:
                        packet[k] = pkt[self.sensor_map[k]]
                logdbg("mapped packet: %s" % packet)
                yield packet
            except Queue.Empty:
                logdbg('empty queue')


class ListenThread(threading.Thread):
    # this thread runs the network code that communicates with the station.
    # it provides the queue on which data are placed, as well as the mechanism
    # to start/stop the network operations.

    def __init__(self, listener, queue):
        threading.Thread.__init__(self)
        self.name = 'observer-listener'
        self._listener = listener
        self._queue = queue

    def stop_running(self):
        self._listener._running = False

    def run(self):
        self._listener.run(self._queue)


class Observer(object):

    DEFAULT_LISTEN_PORT = 6500
    MAX_DATA = 1024
    BROADCAST_PORT = 6000
    SEARCH_MSG = 'PC2000\x00\x00SEARCH\x00\x00\x00\xcd\xfd\x94,\xfb\xe3\x0b\x0c\xfb\xe3\x0bP\xab\xa5w\x00\x00\x00\x00\x00\xdd\xbfw'
    QUERY_MSG = 'PC2000\x00\x00READ\x00\x00\x00\x00NOWRECORD\x00\x00\x00\x00\x00\x00\x00\xb8\x01\x00\x00\x00\x00\x00\x00'

    def __init__(self, host='', port=DEFAULT_LISTEN_PORT,
                 poll_interval=10, timeout=10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.sock = None
        self._running = False

    def setup(self):
        # create a socket server that listens for connections.
        if self.sock is None:
            try:
                logdbg("create socket")
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.sock.settimeout(self.timeout)
                logdbg("bind socket")
                self.sock.bind((self.host, self.port))
            except socket.error, e:
                logdbg("socket setup failed: %s" % e)
                self.sock = None

    def teardown(self):
        # shut down the socket server.
        if self.sock is not None:
            logdbg("close socket")
            self.sock.close()
            self.sock = None

    def run(self, queue):
        self._running = True
        while self._running:
            try:
                self.setup()
                self.send_broadcast()
                while self._running:
                    for data in self.get_data():
                        if data is not None:
                            queue.put(data)
                        if not self._running:
                            break
            except socket.error, e:
                logerr("socket failure: %s" % e)
            self.teardown()
            time.sleep(5)

    def get_data(self):
        # when we get a connection, query the client over the socket for data.
        # when we get the data, yield it.  if the socket fails for any reason,
        # fall back to listening.
        #
        # this is implemented as a generator, so the caller can simply loop on
        # the data that this method receives.  when data is none, the caller
        # can bail out, or re-invoke this method to start the process over.
        conn = None
        try:
            conn, addr = self.sock.accept()
            logdbg("got connection from %s" % addr)
            while True:
                try:
                    logdbg("sending query to %s" % addr)
                    conn.send(Observer.QUERY_MSG)
                    data = conn.recv(Observer.MAX_DATA)
                    logdbg("received data from %s: %s" % (raddr, _fmt(data)))
                    yield data
                    time.sleep(self.poll_interval)
                except socket.timeout:
                    logdbg("timeout while querying/receiving")
                    yield None
        except socket.error, e:
            logdbg("get_data fail: %s" % e)
            raise
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def send_broadcast():
        # broadcast a udp message
        addr = '255.255.255.255' # <broadcast>
        port = Observer.BROADCAST_PORT
        logdbg("broadcast to %s:%s" % (host, port))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        logdbg("broadcast search message: %s" % _fmt(Observer.SEARCH_MSG))
        s.sendto(Observer.SEARCH_MSG, (addr, port))
        logdbg("close broadcast socket")
        s.close()

    @staticmethod
    def decode_data(data):
        #  0 A8: 8 character string
        #  1 A8: 8 character string
        #  2 Z16: 16 characters null-terminated   NOWRECORD
        #  3 S: 1 16-bit unsigned short
        #  4 C: 1 8-bit unsigned char
        #  5 I: 1 16-bit unsigned int
        #  6 C: 1 8-bit unsigned char
        #  7 S: 1 16-bit unsigned short           wind_dir
        #  8 C2: 2 8-bit unsigned char            humidity_in %
        #  9                                      humidity_out %
        # 10 f14: 14 single precision float       temperature_in F
        # 11
        # 12                                      barometer inHg
        # 13                                      temperature_out F
        # 14                                      dewpoint F
        # 15                                      windchill F
        # 16                                      wind_speed mph
        # 17                                      wind_gust mph
        # 18                                      rain_hour in
        # 19
        # 20
        # 21
        # 22                                      rain_year in
        # 23                                      luminosity lux
        # 24 C2: 2 8-bit unsigned char            uv
        # 45
        logdbg("decode data: %s" % _fmt(data))
        pkt = dict()
        try:
            pkt['debug'] = None # placeholder
        except Exception, e:
            logdbg("decode failed: %s" % e)
        return pkt


if __name__ == '__main__':
    import optparse

    usage = """%prog [options] [--debug] [--help]"""

    def main():
        syslog.openlog('wee_observer', syslog.LOG_PID | syslog.LOG_CONS)
        parser = optparse.OptionParser(usage=usage)
        parser.add_option('--version', dest='version', action='store_true',
                          help='display driver version')
        parser.add_option('--debug', dest='debug', action='store_true',
                          help='display diagnostic information while running')
        parser.add_option('--host', dest='host', metavar="HOST",
                          help='ip address of interface on which to listen',
                          default='')
        parser.add_option('--port', dest='port', type=int, metavar="PORT",
                          help='port on which driver should listen',
                          default=Observer.DEFAULT_LISTEN_PORT)
        parser.add_option('--test-decode', dest='filename', metavar='FILENAME',
                          help='test the decoding')
        (options, _) = parser.parse_args()

        if options.version:
            print "driver version %s" % DRIVER_VERSION
            exit(1)

        if options.debug is not None:
            syslog.setlogmask(syslog.LOG_UPTO(syslog.LOG_DEBUG))
        else:
            syslog.setlogmask(syslog.LOG_UPTO(syslog.LOG_INFO))

        if options.filename:
            data = ''
            with open(options.filename, "r") as f:
                data = f.read()
            print Observer.decode_data(data)
            exit(0)

        print "listen on %s:%s" % (options.host, options.port)
        station = Observer(options.host, options.port)
        queue = Queue.Queue()
        t = ListenThread(station, queue)
        t.start()
        while True:
            try:
                data = queue.get(True, 10)
                print "raw data:", _fmt(data)
                pkt = Observer.decode_data(data)
                print "raw packet:", pkt
                time.sleep(10)
            except Queue.Empty:
                pass
            except KeyboardInterrupt:
                t.stop_running()
                break
        t.join()

    main()

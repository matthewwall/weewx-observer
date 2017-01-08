weewx-observer
Copyright Matthew Wall, all rights reserved
Distributed under terms of the GPLv3

This is a driver for weewx that captures data by polling an observer weather
station over wifi.


===============================================================================
Installation

1) download the driver

wget -O weewx-observer.zip https://github.com/matthewwall/weewx-observer/archive/master.zip

2) install the driver

sudo wee_extension --install weewx-observer.zip

3) configure the driver

sudo wee_config --reconfigure driver=user.observer --no-prompt

4) start weewx

sudo /etc/init.d/weewx start

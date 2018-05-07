import re
import threading
import time

from printrun.printcore import printcore
from printrun.eventhandler import PrinterEventHandler


# ##################################################################################################
#
# Vars
#
# ####
DEBUG  = True
PREHEAT_BED  = True
RESET_DEFAULTS = True

BED_TEMP_C = 50
COM_PORT = 'COM8'
TOLERANCE_MM = 0.035
DEFAULT_DELTA_RADIUS = 61.7
Z_OFFSET = .55

CMD_COMPLETE_REGEX = 'ok N0 P15 B15'
G29_REGEX = 'Bed X\: (.*) Y\: (.*) Z\: (.*)'
M666_REGEX = 'M666 X(.*) Y(.*) Z(.*)'
M665_REGEX = 'M665 L(.*) R(.*) S(.*)'


# ##################################################################################################
#
# Helpers
#
# ####

# --------------------------------------------------------------------------------------------------
def debug(line):
    if DEBUG:
        print('[DEBUG] :: {0}'.format(line.strip()))

# --------------------------------------------------------------------------------------------------
def log(line):
    print('[LOG] :: {0}'.format(line.strip()))


# ##################################################################################################
#
# BasicHandler
#
# ####
class BasicHandler(PrinterEventHandler):
    def __init__(self, lock):
        self.lock = lock
        self.line = ""
        self.error = False

    def on_init(self):
        debug('init')

    def on_send(self, command, gline):
        debug('send {0}'.format(command))

    def on_recv(self, line):
        debug('recv {0}'.format(line))

        self.last_line = line.strip()
        self.error = False

        with self.lock:
            self.lock.notify()

    def on_connect(self):
        debug('connect')

    def on_disconnect(self):
        debug('disconnect')

    def on_error(self, error):
        debug('error {0}'.format(error))

        self.error = True
        self.last_line = line.strip()

        with self.lock:
            self.lock.notify()

    def on_online(self):
        debug('online')

        self.error = False

        with self.lock:
            self.lock.notify()

    def on_temp(self, line):
        debug('temp {0}'.format(line))


# ##################################################################################################
#
# Send the printhead to home position (G28)
#
# ####
def go_home(printer, lock, handler):
    printer.send_now('G28')
    with lock:
        lock.wait()


# ##################################################################################################
#
# Perform the bend autoleveling function (G29)
#
# ####
def run_autolevel(printer, lock, handler):
    printer.send_now('G29 P2 V4')

    results = []

    # should get 13 outputs back
    for x in range(0, 13):
        with lock:
            lock.wait()

            m = re.search(CMD_COMPLETE_REGEX, handler.last_line)
            if m:
                return results

            # response recieved, parse it
            m = re.search(G29_REGEX, handler.last_line)
            if m:
                groups = m.groups()
                if len(groups) == 3:
                    results.append(float(groups[2]))

    return results


# ##################################################################################################
#
# Heat the bed to the specified temperature in C (M140)
#
# ####
def heat_bed(printer, lock, handler, temp):
    printer.send_now('M190 S{0:.4f}'.format(temp))

    heating = True
    while heating:
        with lock:
            lock.wait()

        m = re.search(CMD_COMPLETE_REGEX, handler.last_line)
        if m:
            heating = False


# --------------------------------------------------------------------------------------------------
def computeVariance(data, status):
    # get averages
    x_total = 0.0
    y_total = 0.0
    z_total = 0.0
    c_total = 0.0

    dp_count = len(data['datapoints']) * 2.0

    for d in data['datapoints']:
        z_total += d['z1']
        z_total += d['z2']
        x_total += d['x1']
        x_total += d['x2']
        y_total += d['y1']
        y_total += d['y2']
        c_total += d['c1']
        c_total += d['c2']

    data['z_avg'] = z_total / dp_count
    data['x_avg'] = x_total / dp_count
    data['y_avg'] = y_total / dp_count
    data['c_avg'] = c_total / dp_count

    if status['ref_axis'] == None:
        log('setting ref_axis...')

        status['ref_axis'] = 'z_avg'
        if data['x_avg'] > data[status['ref_axis']]:
            status['ref_axis'] = 'x_avg'
        if data['y_avg'] > data[status['ref_axis']]:
            status['ref_axis'] = 'y_avg'

        log('ref_axis == {0}'.format(status['ref_axis']))

    data['high_point'] = data[status['ref_axis']]
    data['c_offset'] = data['c_avg'] - data['high_point']

    # reset to compute square differences
    x_total = 0.0
    y_total = 0.0
    z_total = 0.0
    c_total = 0.0

    for d in data['datapoints']:
        z_total += pow(d['z1'] - data['z_avg'], 2)
        z_total += pow(d['z2'] - data['z_avg'], 2)
        x_total += pow(d['x1'] - data['x_avg'], 2)
        x_total += pow(d['x2'] - data['x_avg'], 2)
        y_total += pow(d['y1'] - data['y_avg'], 2)
        y_total += pow(d['y2'] - data['y_avg'], 2)
        c_total += pow(d['c1'] - data['c_avg'], 2)
        c_total += pow(d['c2'] - data['c_avg'], 2)

    data['z_var'] = z_total / dp_count
    data['x_var'] = x_total / dp_count
    data['y_var'] = y_total / dp_count
    data['c_var'] = c_total / dp_count

# --------------------------------------------------------------------------------------------------
def printReport(data, status):
    log('Reference axis: {0}'.format(status['ref_axis']))
    log('Z (avg: {0:.4f}, variance: {1:.6f})'.format(data['z_avg'], data['z_var']))
    log('X (avg: {0:.4f}, variance: {1:.6f})'.format(data['x_avg'], data['x_var']))
    log('Y (avg: {0:.4f}, variance: {1:.6f})'.format(data['y_avg'], data['y_var']))
    log('C (avg: {0:.4f}, variance: {1:.6f})'.format(data['c_avg'], data['c_var']))

# --------------------------------------------------------------------------------------------------
def runAdjustments(printer, lock, handler, data):
    adjust_endstops = False
    if abs(data['high_point'] - data['z_avg']) > TOLERANCE_MM:
        log('z_offset too great: {0:.4f}'.format(data['high_point'] - data['z_avg']))
        adjust_endstops = True
    if abs(data['high_point'] - data['x_avg']) > TOLERANCE_MM:
        log('x_offset too great: {0:.4f}'.format(data['high_point'] - data['x_avg']))
        adjust_endstops = True
    if abs(data['high_point'] - data['y_avg']) > TOLERANCE_MM:
        log('y_offset too great: {0:.4f}'.format(data['high_point'] - data['y_avg']))
        adjust_endstops = True

    if adjust_endstops:
        adjustOffsets(printer, lock, handler, data)
        return False

    if abs(data['c_offset']) > TOLERANCE_MM:
        log('c_offset too great: {0:.4f}'.format(data['c_offset']))
        adjustDeltaRadius(printer, lock, handler, data)
        return False


    return True

# --------------------------------------------------------------------------------------------------
def adjustOffsets(printer, lock, handler, data):
    z_offset = data['z_avg'] - data['high_point']
    x_offset = data['x_avg'] - data['high_point']
    y_offset = data['y_avg'] - data['high_point']

    printer.send_now('M666 Z{0:.4f} X{1:.4f} Y{2:.4f}'.format(
        data['M666']['Z'] + z_offset,
        data['M666']['X'] + x_offset,
        data['M666']['Y'] + y_offset,
    ))

    with lock:
        lock.wait()

# --------------------------------------------------------------------------------------------------
def adjustDeltaRadius(printer, lock, handler, data):
    printer.send_now('M665 R{0:.4f}'.format(
        (data['M665']['R'] - (data['c_offset']*2)),
    ))

    with lock:
        lock.wait()

# --------------------------------------------------------------------------------------------------
def queryPrinter(printer, lock, handler, data):
    printer.send_now('M503')

    reading = True
    while reading:
        with lock:
            lock.wait()

        m = re.search(CMD_COMPLETE_REGEX, handler.last_line)
        if m:
            reading = False
            continue

        m = re.search(M666_REGEX, handler.last_line)
        if m:
            groups = m.groups()
            data['M666'] = {}
            data['M666']['X'] = float(groups[0])
            data['M666']['Y'] = float(groups[1])
            data['M666']['Z'] = float(groups[2])
            continue

        m = re.search(M665_REGEX, handler.last_line)
        if m:
            groups = m.groups()
            data['M665'] = {}
            data['M665']['L'] = float(groups[0])
            data['M665']['R'] = float(groups[1])
            data['M665']['S'] = float(groups[2])
            continue

    log('X {0:.4f}, Y {1:.4f}, Z {2:.4f}, L {3:.4f}, R {4:.4f}, S {5:.4f}'.format(
            data['M666']['X'],
            data['M666']['Y'],
            data['M666']['Z'],
            data['M665']['L'],
            data['M665']['R'],
            data['M665']['S'],
    ))

# --------------------------------------------------------------------------------------------------
def fixDeltaCalibration(printer, lock, handler, status):
    go_home(printer, lock, handler)

    for i in range(0, 3):
        data = {
            'datapoints' : [],
            'high_point' : 0.0,
            'c_offset'   : 0.0,
            'z_avg'      : 0.0,
            'z_var'      : 0.0,
            'x_avg'      : 0.0,
            'x_var'      : 0.0,
            'y_avg'      : 0.0,
            'y_var'      : 0.0,
            'c_avg'      : 0.0,
            'c_var'      : 0.0,
        }

        results = run_autolevel(printer, lock, handler)
        if len(results) == 8:
            data['datapoints'].append({
                'z1'    : results[0],
                'z2'    : results[1],
                'z_avg' : (results[0] + results[1]) / 2.0,
                'x1'    : results[2],
                'x2'    : results[3],
                'x_avg' : (results[2] + results[3]) / 2.0,
                'y1'    : results[4],
                'y2'    : results[5],
                'y_avg' : (results[4] + results[5]) / 2.0,
                'c1'    : results[6],
                'c2'    : results[7],
                'c_avg' : (results[6] + results[7]) / 2.0,
            })
        else:
            raise Exception('invalid result count from bed autolevel routine ({0}) :: {1}'.format(
                len(results), 
                results,
            ))

    log('run complete. computing variance...')
    computeVariance(data, status)
    printReport(data, status)

    # done once all bed points are within the configured threshold of each other
    queryPrinter(printer, lock, handler, data)
    status['converged'] = runAdjustments(printer, lock, handler, data)

    return data

# --------------------------------------------------------------------------------------------------
def save_settings(printer, lock, handler):
    printer.send_now('M500')

    with lock:
        lock.wait()

# --------------------------------------------------------------------------------------------------
def set_defaults(printer, lock,  handler):
    printer.send_now('M666 X0 Y0 Z0')
    with lock:
        lock.wait()

    printer.send_now('M665 R{0:.4f}'.format(DEFAULT_DELTA_RADIUS))
    with lock:
        lock.wait()

    printer.send_now('M206 Z0')
    with lock:
        lock.wait()

# --------------------------------------------------------------------------------------------------
def set_z_offset(printer, lock, handler, data):
    hp = data['z_avg']
    if data['x_avg'] > hp:
        hp = data['x_avg']
    if data['y_avg'] > hp:
        hp = data['y_avg']

    zo = -(hp + Z_OFFSET)

    log('Setting Z Offset to {0:.4f}'.format(zo))
    
    printer.send_now('M206 Z{0:.4f}'.format(zo))
    with lock:
        lock.wait()

# --------------------------------------------------------------------------------------------------
def sendCommands(printer, lock, handler):
    # wait for the printer to come online
    with lock:
        lock.wait() # condition var appears to start off signaled? swallow the initial notify
        lock.wait()

    if RESET_DEFAULTS:
        set_defaults(printer, lock, handler)

    if PREHEAT_BED:
        heat_bed(printer, lock, handler, BED_TEMP_C)

    status = {
        'converged' : False,
        'ref_axis' : None,
    }

    data = None

    while not status['converged']:
        data = fixDeltaCalibration(printer, lock, handler, status)

    set_z_offset(printer, lock, handler, data)
    save_settings(printer, lock, handler)
    go_home(printer, lock, handler)

# entry point
# --------------------------------------------------------------------------------------------------
def main():
    lock=threading.Condition()

    handler=BasicHandler(lock)

    printer=printcore()
    printer.addEventHandler(handler)

    t = threading.Thread(target=sendCommands, args=(printer, lock, handler))
    t.start()

    printer.connect(COM_PORT, 115200)

    while printer.online == False:
        log('Waiting to connect...')
        time.sleep(1)

    t.join()

    printer.disconnect()


# bootstrap
# --------------------------------------------------------------------------------------------------
if __name__ == '__main__':
    main()

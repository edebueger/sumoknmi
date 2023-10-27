#!/usr/bin/env python3 
# Erik de Bueger

import os
import sys
import datetime
import logging
import optparse
from random import randint
import re
import json
import boto3
import pickle
import requests

standarddateformat  = '%Y%m%d'

# Daily data
URLS = {'daily': 'https://www.daggegevens.knmi.nl/klimatologie/daggegevens',
            'hourly': 'https://www.daggegevens.knmi.nl/klimatologie/uurgegevens',
            'daily_rain': 'https://www.daggegevens.knmi.nl/klimatologie/monv/reeksen'}

# STN      LON(east)   LAT(north)     ALT(m)  NAME
# 235:         4.785       52.924       0.50  DE KOOY
# 209         4.518       52.465      0.00        IJmond
STATIONREGEX=re.compile(r'^#\s*(?P<NUM>[0-9]+)\s+(?P<LON>[0-9\.]+)\s+(?P<LAT>[0-9\.]+)\s+(?P<ALT>[0-9\.-]+)\s+(?P<Station>.*)$', re.U)
# EV24     = Potential evapotranspiration (Makkink) (in 0.1 mm);
LEGENDREGEX=re.compile(r'^#\s+(?P<SYMBOL>[A-Z0-9]+)\s+:\s*(?P<legend>.*)$', re.U)
## STN,YYYYMMDD,DDVEC,FHVEC,   FG,  FHX, FHXH,  FHN, FHNH,  FXX, FXXH,   TG,   TN,  TNH,   TX,  TXH, T10N,T10NH,   SQ,   SP,    Q,   DR,   RH,  RHX, RHXH,   PG,   PX,  PXH,   PN,  PNH,  VVN, VVNH,  VVX, VVXH,   NG,   UG,   UX,  UXH,   UN,  UNH, EV24
HEADERREGEX=re.compile(r'^#\s+STN,\s*YYYYMMDD,(?P<header>.*)$', re.U)
#  209,20231019,  145,   60,   70,  100,   24,   50,   11,  130,    1,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     ,     , 
DATAREGEX=re.compile(r'^(\s*[0-9]*,){1,}(\s*[0-9]*){0,1}$', re.U)

# {event type:date}
class Cache(object) :
    def __init__(self, region,logger,starttime):
        self.logger    = logger
        self.starttime = starttime
        self.counter   = 0
        self.cache     =  None
        self.d         = {}
    def Get(self,key) :
        return(self.starttime)
    def Write(self,key, value):
        self.d[key]=value
        self.counter+=1
        if self.counter % 10 == 0:
            self.Flush()
    def Close(self) :
        self.Flush()
    def Flush(self) :
        pass
    def Print(self,fp) :
        for k,v in self.d.items() :
            fp.write('{} -- {}\n'.format(k,v.strftime(standarddateformat)))

class CacheFile(Cache) :
    def __init__(self, region,logger,starttime):
        self.logger = logger
        self.starttime = starttime
        self.counter = 0
        self.file = region
        self.fp = None
        self.d={}
        try:
            self.fp = open(os.path.join(sys.path[0],self.file),'rb')
            # use dict as fast cache
            self.d = pickle.load(self.fp)
            self.fp.close()
        except Exception as e:
            self.logger.warning(str(e))
            self.logger.warning('Filecache could not be opened: {}, will create later'.format(os.path.join(sys.path[0],self.file)))
            self.d={}

    def Get(self,key) :
        try:
            return(self.d[key])
        except KeyError:
            # Not in cache, use earliest date
            return(self.starttime)

    # write dict to cache
    def Flush(self) :
        try:
            self.fp = open(os.path.join(sys.path[0],self.file),'wb')
            pickle.dump(self.d,self.fp)
            self.fp.truncate()
            self.fp.flush()
            self.fp.close()
        except Exception as e:
            self.logger.warning(str(e))
            self.logger.warning('Error writing last_eventid to cache')

class CacheAWSParameter(Cache) :
    def __init__(self,region,logger,starttime):
        self.logger    = logger
        self.starttime = starttime
        self.counter   = 0
        self.cache     = boto3.client('ssm', region_name=region)
        self.d         = {}

    # Check last event tokens or timestamps per key in AWS cache
    # key is event type, value is date: knmi-daily:20211123
    def Get(self,key) :
        # First try local cache
        try:
            return(self.d[key])
        except KeyError:
            pass
        # Retrieve from remote cache
        cachekey = 'knmi-{}'.format(key)
        try:
            cachevalue = self.cache.get_parameter(Name=cachekey)['Parameter']['Value']
            # We have a string back from the cache, should be timestamp
            # Try timestamp
            self.d[key] = datetime.datetime.strptime(cachevalue, standarddateformat)
            return(self.d[key])
        except Exception as e:
            # No cache value found
            self.logger.warning(str(e))
            self.logger.warning('Error: {} not found in cache! Starting from {}. \n'.format(cachekey,self.starttime))
            return(self.starttime)

    # write dict to cache
    def Flush(self) :
        for k,v in self.d.items() :
            cachekey = 'knmi-{}'.format(k)
            try:
                # try date format
                x = self.cache.put_parameter(Name=cachekey, Type='String', Overwrite=True,Value=v.strftime(standarddateformat))
            except Exception as e:
                self.logger.warning(str(e))
                self.logger.warning('Error writing last_eventid to cache')

def MarshalGeneric(d) :
    # Remove emty fields
    if d==None: return(d)
    d=dict((k, v) for k, v in d.items() if v)
    for k,v in d.items() :
        try:
            d[k]=int(d[k])
        except:
            pass
    try:
        d['timestamp'] = datetime.datetime.strptime(str(d['YYYYMMDD']), standarddateformat).strftime('%Y-%m-%d:%H%M%S')
    except :
        pass
    return(d)

# Class for writing events to destination, either Splunk or Elastic.
# Overwrites possible for alternate destinations
class EventWriter(object) :
    def __init__(self, **kwargs):
        self.marshal  = kwargs.get('mfunc',None)
        self.logger   = kwargs.get('logger',None)

    # Send a kv-formatted message to stdout
    # message is json dict
    def send(self, message):
        if self.marshal != None :
            data=self.marshal(message)
        else :
            data=message
        #    data = message.encode('utf-8', 'replace')
        #sys.stdout.write('{}\n\n'.format(json.dumps(data).encode('utf-8', 'replace')))
        sys.stdout.write('{}\n\n'.format(json.dumps(data)))

class EventWriterJSON(EventWriter) :
    def __init__(self, **kwargs):
        self.marshal  = kwargs.get('mfunc',None)
        self.logger   = kwargs.get('logger',None)
        self.url      = kwargs.get('endpoint',None)

    # message is dict
    def send(self, message):
        if self.marshal != None :
            data=self.marshal(message)
        else :
            data=message
        response = requests.post(self.url, data=json.dumps(data))
        if self.logger != None:
            self.logger.info('Endpoint response: {}'.format(response.text))

def GetDailyData(url, startdate,enddate, logger) :
    params = dict(stns='ALL')
    start = startdate.strftime(standarddateformat)
    end = enddate.strftime(standarddateformat)
    params.update(start=start)
    params.update(end=end)
    params.update(vars='ALL')
    r = requests.post(url=url, data=params)
    if r.status_code != 200:
        raise requests.HTTPError(r.status_code, url, params)
        return(None)
    return(r)

# Iterator over KNMI csv format file, returns dicts
def data_digger(raw):
    cols=['STN','YYYYMMDD']
    headerseen=False
    for line in raw.text.splitlines():
        if headerseen:
            m=DATAREGEX.match(line)
            if m:
                lColumns=[x.strip() for x in line.lstrip().rstrip().split(',')]
                dFormat=dict(list(zip(cols,lColumns)))
                yield(dFormat)
        else:
            m=HEADERREGEX.match(line)
            if m:
                headerseen=True
                cols=cols+[x.strip() for x in m.group('header').split(',')]

# Iterator over KNMI csv format file, returns dicts
def location_digger(raw):
    for line in raw.text.splitlines():
        m=HEADERREGEX.match(line)
        if m:
            return
        m =  STATIONREGEX.match(line)
        if m != None:
            d = {'STN':int(m.group('NUM')), 'lat':m.group('LAT'), 'lon':m.group('LON'), 'alt':m.group('ALT')}
            yield (d)

def legend_digger(raw):
    for line in raw.text.splitlines():
        m=HEADERREGEX.match(line)
        if m:
            return
        m=LEGENDREGEX.match(line)
        if m:
            try:
                yield({'symbol':m.group('SYMBOL'),'desc':m.group('legend')})
            except:
                pass

def station_digger(raw):
    for line in raw.text.splitlines():
        m=HEADERREGEX.match(line)
        if m:
            return
        m=STATIONREGEX.match(line)
        if m :
            # we have a station spec
            try:
                yield({'STN':int(m.group('NUM')),'LON':m.group('LON'), 'LAT':m.group('LAT'), 'ALT':m.group('ALT'), 'Station':m.group('Station').strip()})
            except:
                pass

# Helper to iterate over days
def daterange(start_date, end_date):
    for n in range(int((end_date - start_date).days)):
        yield start_date + datetime.timedelta(n)

# Main code
def main():
    # Parse command line options
    p = optparse.OptionParser("usage: %prog [options]")
    p.add_option("-u", "--url", dest="url", default=URLS['daily'], \
                     help = 'url for dutch KNMI daily data extraction, defaults to: {}\nAlternate option: {}'.format(URLS['daily'], URLS['hourly']))
    p.add_option("-y", "--hourly", dest="hourly", action='store_true', default=False, \
                     help = 'Extract hourly data as opposed to daily, defaults to: {}'.format(False))
    p.add_option("-t", "--stations", dest="stations", action='store_true', default=False, \
                 help='Extract list of stations and write to stdout, default=False')
    p.add_option("-g", "--legend", dest="legend", action='store_true', default=False, \
                 help='send legend output only, default=False')
    p.add_option("-f", "--format", dest="format", default='stdout', \
                 help='send output to {}, default output is stdout'.format('sumo,splunk,elastic, awselastic,json or stdout'))
    p.add_option("--latest", dest="latest", type='int', default=2, \
                     help = 'Collect this many days in the past as the latest available data, more recent days will not be populated, defaults to 30')
    p.add_option("--history", dest="history", type='int', default=30, \
                     help = 'Collect this many days of historic data, defaults to 10')
    p.add_option("--maxdays", dest="maxdays", type='int', default=5, \
                     help = 'Maximum amount of days to be collected in one run , defaults to 5')
    p.add_option("--endpoint", dest="endpoint", default=None, \
                  help = 'endpoint (sumo, splunk, elastic url) where data is delivered, defaults to: {}'.format('None'))
    p.add_option("-n", "--stationnumber", dest="stationnumber", type='int', default=-1, \
                  help = 'Collect data for specific station, identified by station number, defaults to -1')
    p.add_option("--nolambda", dest="nolambda", action='store_true', default=False, \
                 help='if set donot run as as lambda function, default=False')
    p.add_option("--region", dest="region", default='us-east-2', \
                     help = 'AWS region, defaults to: {}'.format('us-east-2'))
    p.add_option("-l", "--log", dest="loglevel", default='WARNING', \
                 help = 'loglevel field, defaults to WARNING, options are: CRITICAL, ERROR, WARNING, INFO, DEBUG, NOTSET')
    p.add_option("--cache", dest="cache", default='lastknmievents', \
                 help = 'name of cache file or endpoint for AWS memcached service, defaults to : lastknmievent')
    opts,args = p.parse_args()

    # For AWS lambda: try to overwrite some options from environment
    for x in ('format','endpoint','logtype','url','loglevel','stationnumber','region','history','latest','maxdays') :
        try:
            exec('opts.{}=os.environ[\'{}\']'.format(x,x))
        except Exception as e:
            pass
    try:
        opts.history = int(opts.history)
    except:
        pass
    try:
        opts.maxdays = int(opts.maxdays)
    except:
        pass
    try:
        opts.latest = int(opts.latest)
    except:
        pass
    try:
        opts.stationnumber = int(opts.stationnumber)
    except:
        pass

    startdate= datetime.datetime.now()-datetime.timedelta(days=opts.history)
    enddate= datetime.datetime.now()-datetime.timedelta(days=opts.latest)

    # Initiate Logging
    numeric_level = getattr(logging, opts.loglevel.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError('Invalid log level: %s' % opts.loglevel)

    formatter = logging.Formatter('%(asctime)s,Level=%(levelname)s,\
                                   LOGGING=%(message)s', '%m/%d/%Y %H:%M:%S')
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(formatter)
    logger        = logging.getLogger('KNMI')
    logger.addHandler(sh)
    logger.level=numeric_level

    # Open up the cache, which remembers per station what the date of the most recent event was
    if opts.nolambda:
        # revert to local file cache
        try:
            cache = CacheFile(opts.cache, logger, startdate)
        except Exception as e:
            # giving up
            logger.warning(str(e)+'\n')
            logger.warning('Could not open file cache, exit')
            return
    else:
        try:
            cache = CacheAWSParameter(opts.region,logger,startdate)
        except Exception as e:
            logger.warning(str(e)+'\n')
            logger.warning('Could not open AWS cache, exit')
            return

    # Retrieve latest date from cache
    cachedate = cache.Get('daily')

    # Create output channel
    if opts.format=='stdout':
        eventwriter=EventWriter(logger=logger,mfunc=MarshalGeneric)
        logger.info('Writing events to stdout')
    elif opts.format=='sumo':
        eventwriter=EventWriterJSON(endpoint=opts.endpoint,mfunc=MarshalGeneric, logger=logger)
        logger.info('Writing events to Sumo')
    else :
        logger.err('Unknow destination format provided: {}. Options are: stdout-splunk-elastic-sumo'.format(opts.format))
        sys.exit(0)

    # Process options
    if opts.stations:
        thedata = GetDailyData(URLS['daily'], startdate,startdate,logger)
        for d in station_digger(thedata):
            if d != None:
                d.update({'type':'station'})
                eventwriter.send(d)
        sys.exit()
    if opts.legend:
        thedata = GetDailyData(URLS['daily'], startdate,startdate,logger)
        for d in legend_digger(thedata):
            if d != None:
                d.update({'type':'legend'})
                eventwriter.send(d)
        sys.exit()
    if opts.stationnumber > 0 :
        thedata = GetDailyData(URLS['daily'], startdate,startdate,logger)
        for d in data_digger(thedata):
            if int(d['STN'])==opts.stationnumber:
                eventwriter.send(d)
                sys.exit()
        sys.exit()

    # Decide which days we will try to collect: startdate=the start date requested, enddate=latest possible end date, cachedate=last date collected, or equal to startdate
    daysback= (datetime.datetime.now()-cachedate).days
    if daysback > opts.maxdays :
        enddate = cachedate + datetime.timedelta(days=opts.maxdays)
    startdate=cachedate + datetime.timedelta(days=1)
    logger.info('Planning to collect from {} to {}'.format(startdate.strftime(standarddateformat), enddate.strftime(standarddateformat)))

    # Write legend + stations inventory only on some random occasions
    writelegend = (randint(1,5)==3)

    # Start collecting on per-day basis
    for thedate in daterange(startdate, enddate):
        thedata = GetDailyData(URLS['daily'], thedate,thedate,logger)
        # Send data
        nEvents=0
        for d in data_digger(thedata):
            if d != None:
                d.update({'type':'daily'})
                eventwriter.send(d)
                nEvents+=1
        if writelegend:
            for d in legend_digger(thedata):
                if d != None:
                    d.update({'type':'legend'})
                    eventwriter.send(d)
            for d in station_digger(thedata):
                if d != None:
                    d.update({'type':'station'})
                    eventwriter.send(d)
            writelegend=False
        # Update cache
        if nEvents > 10:
            cache.Write('daily',thedate)
        logger.info('Send {} events for day {}'.format(nEvents,thedate.strftime(standarddateformat)))

    cache.Close()

if __name__ == '__main__':
    main()

def lambda_handler(event, context):
    main()
    return {
        'statusCode': 200,
        'body': json.dumps('KNMI Lambda returns\n')
    }

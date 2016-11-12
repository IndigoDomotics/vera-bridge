#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################

import socket
import threading
import urllib2, httplib
import traceback
from datetime import datetime, time
import Queue
import time
import simplejson as json
import copy
import indigo

################################################################################
# Globals
################################################################################
# if we ever want to get more details about a device, including command classes and some other junk, we can use this URL:
#   /data_request?id=status&output_format=json&DeviceNum=5
# the last param is the 'id' value in the lu_sdata return for each device
#
kPollingUrl = u"/data_request?id=lu_sdata&output_format=json"
kActionUrl = u"data_request?id=lu_action&output_format=json"
kRunSceneServiceString = "SceneNum=%i&serviceId=urn:micasaverde-com:serviceId:HomeAutomationGateway1&action=RunScene"
kOnOffServiceString = "DeviceNum=%i&serviceId=urn:upnp-org:serviceId:SwitchPower1&action=SetTarget&newTargetValue=%i"
kBrightnessServiceString = "DeviceNum=%i&serviceId=urn:upnp-org:serviceId:Dimming1&action=SetLoadLevelTarget&newLoadlevelTarget=%i"
kLockServiceString = "DeviceNum=%i&serviceId=urn:micasaverde-com:serviceId:DoorLock1&action=SetTarget&newTargetValue=%i"
kThermostatServiceString_HeatSetpoint = "DeviceNum=%i&serviceId=urn:upnp-org:serviceId:TemperatureSetpoint1_Heat&action=SetCurrentSetpoint&NewCurrentSetpoint=%i"
kThermostatServiceString_CoolSetpoint = "DeviceNum=%i&serviceId=urn:upnp-org:serviceId:TemperatureSetpoint1_Cool&action=SetCurrentSetpoint&NewCurrentSetpoint=%i"
kThermostatServiceString_Mode = "DeviceNum=%i&serviceId=urn:upnp-org:serviceId:HVAC_UserOperatingMode1&action=SetModeTarget&NewModeTarget=%s"
kThermostatServiceString_FanMode = "DeviceNum=%i&serviceId=urn:upnp-org:serviceId:HVAC_FanOperatingMode1&action=SetMode&NewMode=%s"
kTimeout = 10
kPollInterval = 3
kFullUpdateInterval = 60 * 30  # do a full update every 30 minutes
kSupportedDeviceTypes = [2, 3, 5, 7]
kVeraDeviceTypeMap = {
	0									: None,
	1									: None,
	2									: [u"veraDimmer", "Dimmer Module"],
	3									: [u"veraAppliance", "On/Off Module"],
	4									: None,
	5									: [u"veraThermostat", "Thermostat"],
	7									: [u"veraLock", "Door Lock"],
	8									: None,
	16									: None,
	17									: None,
	18									: None,
	21									: None,
}
kThermostatModes = {
	"Off"				: "Off",
	"Cool"				: "CoolOn",
	"Heat"				: "HeatOn",
	"HeatCool"			: "AutoChangeOver",
}
kThermostatFanModes = {
	"Auto"		: "Auto",
	"AlwaysOn"	: "ContinuousOn",
}
kCommand_RunScene = "runScene"
kCommand_TurnOn = "turnOn"
kCommand_TurnOff = "turnOff"
kCommand_SetBrightness = "setBrightness"
kCommand_Lock = "lock"
kCommand_Unlock = "unlock"
kCommand_SetHeatSetpoint = "setHeatSetpoint"
kCommand_SetCoolSetpoint = "setCoolSetpoint"
kCommand_SetThermostatMode = "setThermostatMode"
kCommand_SetThermostatFanMode = "setThermostatFanMode"
kErrorStates = ["2", "3"]

def modelForDeviceInfo(deviceInfo):
	devCategory = deviceInfo.get("category", None)
	if devCategory in kVeraDeviceTypeMap:
		return kVeraDeviceTypeMap[devCategory]
	else:
		return None

################################################################################
class Vera(threading.Thread):

	def __init__(self, address, port=3480, standardLogMethod=None, debugLogMethod=None):
		threading.Thread.__init__(self)
		self.address = address
		self.port = port
		# temporarily set a very short timeout just to test to see if the vera is out there
		socket.setdefaulttimeout(2)
		# try to open the standard info url - it will throw if there's a problem and that's OK
		theUrl = "http://%s:%i/%s" % (self.address, self.port, kPollingUrl)
		f = urllib2.urlopen(theUrl)
		f.close()
		# set the timeout used for the rest of the execution of this thread
		socket.setdefaulttimeout(kTimeout)
		self.standardLogMethod = standardLogMethod
		self.debugLogMethod = debugLogMethod
		self.state = -1
		self.version = ""
		self.model = ""
		self.serial = ""
		self.lastLoadTime = 0
		self.lastDataVersion = 0
		self.scenes = {}
		self.devices = {}
		self.lastPoll = 0
		self.updateQueue = Queue.Queue()
		self.shouldContinue = True
		self.commandQueue = None
		self.fullUpdateNow = True
		self.lastFullUpdate = 0
		self.threadDebug = False
	
	########################################
	def logMethod(self, output, isError=False, isDebug=True):
		if isError:
			isDebug=False		
		if isDebug:
			if self.threadDebug:
				if self.debugLogMethod:
					self.debugLogMethod("vera thread: %s" % output)
				else:
					print output
		elif self.standardLogMethod:
			self.standardLogMethod("vera thread: %s" % output, isError=isError)
		else:
			print "vera thread: %s" % output
	
	########################################
	def stop(self):
		self.shouldContinue = False
		
	########################################
	def doFullUpdate(self):
		self.fullUpdateNow = True
			
	########################################
	def setThreadDebug(self, debug):
		self.threadDebug = debug
			
	########################################
	def run(self):
		self.logMethod("starting run loop: debugging: %s" % ("True" if self.threadDebug else "False"))
		try:
			while self.shouldContinue:
				queueHasItems = True
				while self.commandQueue and self.shouldContinue and queueHasItems and not self.fullUpdateNow:
					try:
						commandDict = self.commandQueue.get_nowait()
						self.logMethod("processing command: %s" % str(commandDict))
						self._processCommand(commandDict)
						self.commandQueue.task_done()
					except:
						queueHasItems = False
				if ((int(time.time()) - self.lastPoll) >= kPollInterval) or self.fullUpdateNow:
					self._update(fullUpdate=self.fullUpdateNow)
					self.lastPoll = int(time.time())
				time.sleep(.1)
		except Exception, e:
			self.logMethod("some exception in the run loop occurred:\n%s" % str(e))
		self.logMethod("exiting run loop")
		
	########################################
	def _update(self, fullUpdate=False):
		self.logMethod("_update: starting at %s" % datetime.today().strftime("%H:%M:%S"))
		if fullUpdate:
			self.lastLoadTime = 0
			self.lastDataVersion = 0
		theUrl = "http://%s:%i/%s&loadtime=%i&dataversion=%i" % (self.address, self.port, kPollingUrl, self.lastLoadTime, self.lastDataVersion)
		self.logMethod("_update: url: %s" % theUrl)
		try:
			f = urllib2.urlopen(theUrl)
			infoDict = json.load(f)
			f.close()
			if infoDict["full"]:
				if self.threadDebug:
					s = json.dumps(infoDict, sort_keys=True, indent=4)
					self.logMethod("_update: doing full update with infoDict:\n\n%s\n\n" % s)
				# First we'll get the scenes
				newSceneDict = {}
				for sceneInfo in infoDict.get("scenes", []):
					self.logMethod("sceneInfo: %s" % sceneInfo)
					newSceneDict[sceneInfo["id"]] = sceneInfo
				self.scenes = newSceneDict
				self.logMethod("_update: scenes:\n%s" % str(self.scenes))
				# Next we'll do the devices
				newDeviceDict = {}
				oldDeviceDict = copy.copy(self.devices)
				for deviceInfo in infoDict.get("devices", []):
					deviceType = deviceInfo["category"]
					if deviceType in kSupportedDeviceTypes:
						deviceId = deviceInfo["id"]
						if deviceId in oldDeviceDict:
							del oldDeviceDict[deviceId]
						self.logMethod("_update: adding update to update queue: %s" % (deviceInfo))
						self.updateQueue.put_nowait({"updateType": "updateDevice", "device": deviceInfo})
						newDeviceDict[deviceId] = deviceInfo
				for device in oldDeviceDict:
					self.logMethod("adding delete to update queue: %s" % (device))
					self.updateQueue.put_nowait({"updateType": "deleteDevice", "device": device})
					pass
				self.devices = newDeviceDict
				self.logMethod("_update: devices:\n%s" % str(self.devices))
				self.fullUpdateNow = False
				self.lastFullUpdate = int(time.time())
			else:
				# Not a full update - so we don't check and notify for deletions, etc.
				for sceneInfo in infoDict.get("scenes", []):
					if sceneInfo["active"]:
						self.scenes[sceneInfo["id"]] = sceneInfo
				for deviceInfo in infoDict.get("devices", []):
					self.logMethod("_update: partial deviceInfo:\n%s" % str(deviceInfo))
					self.updateQueue.put_nowait({"updateType": "updateDevice", "device": deviceInfo})
				# if we're over 30 minutes from the last full update, do it now
				if (int(time.time()) - kFullUpdateInterval) > self.lastFullUpdate:
					self.fullUpdateNow = True
			self.lastLoadTime = infoDict.get("loadtime", 0)
			self.lastDataVersion = infoDict.get("dataversion", 0)
		except urllib2.URLError, e:
			self.logMethod("_update: url open error:\n%s" % traceback.format_exc(10))
		except httplib.BadStatusLine, e:
			self.logMethod("The Vera isn't responding correctly. Make sure it's available. If it's performing a software upgrade, wait until it's finished then restart the plugin.")
		except KeyError, e:
			self.logMethod("_update: key error:\n%s" % traceback.format_exc(10))
		except Exception, e:
			self.logMethod("_update: vera update error: %s" % traceback.format_exc(10), isError=True)
		finally:
			self.logMethod("_update: ending at %s" % datetime.today().strftime("%H:%M:%S"))

				
	########################################
	def _executeUrl(self, url, deviceName, command):
		try:
			self.logMethod(u"_execute url: %s" % url)
			f = urllib2.urlopen(url)
			infoDict = json.load(f)
			f.close()
			self.logMethod(u"sent \"%s\" %s" % (deviceName, command), isDebug=False)
		except Exception, e:
			self.logMethod(u"send command error: %s" % traceback.format_exc(10), isError=True)
		
	########################################
	def _processCommand(self, commandDict):
		self.logMethod("_processCommand called")
		command = commandDict["command"]
		if command == kCommand_RunScene:
			# execute the scene
			sceneId = commandDict["id"]
			scene = self.scenes.get(sceneId, None)
			if scene and bool(scene["active"]):
				theUrl = "http://%s:%i/%s&%s" % (self.address, self.port, kActionUrl, kRunSceneServiceString % sceneId)
				self._executeUrl(theUrl, scene["name"], "run scene")
			else:
				self.logMethod(u"send command error: scene %i does not exist or is inactive" % sceneId, isError=True)
		else:
			# since it's not a run scene command then it's a device command
			self.logMethod("_processCommand: performing device command")
			deviceId = commandDict["id"]
			if deviceId not in self.devices:
				self.logMethod(u"send command error: device %i does not exist" % deviceId, isError=True)
				self.updateQueue.put_nowait({"updateType": "deleteDevice", "device": {"id": deviceId}})
			else:
				deviceName = self.devices[deviceId]["name"]

				if command == kCommand_TurnOff:
					theUrl = "http://%s:%i/%s&%s" % (self.address, self.port, kActionUrl, kOnOffServiceString % (deviceId, 0))
					self._executeUrl(theUrl, deviceName, "off")
				elif command == kCommand_TurnOn:
					theUrl = "http://%s:%i/%s&%s" % (self.address, self.port, kActionUrl, kOnOffServiceString % (deviceId, 1))
					self._executeUrl(theUrl, deviceName, "on")
				elif command == kCommand_SetBrightness:
					theUrl = "http://%s:%i/%s&%s" % (self.address, self.port, kActionUrl, kBrightnessServiceString % (deviceId, commandDict["value"]))
					self._executeUrl(theUrl, deviceName, "on to %i" % commandDict["value"])

				elif command == kCommand_SetHeatSetpoint:
					theUrl = "http://%s:%i/%s&%s" % (self.address, self.port, kActionUrl, kThermostatServiceString_HeatSetpoint % (deviceId, commandDict["value"]))
					self.logMethod("_processCommand: url: %s" % theUrl)
					self._executeUrl(theUrl, deviceName, "set heat setpoint to %i" % commandDict["value"])
				elif command == kCommand_SetCoolSetpoint:
					theUrl = "http://%s:%i/%s&%s" % (self.address, self.port, kActionUrl, kThermostatServiceString_CoolSetpoint % (deviceId, commandDict["value"]))
					self.logMethod("_processCommand: url: %s" % theUrl)
					self._executeUrl(theUrl, deviceName, "set heat setpoint to %i" % commandDict["value"])
				elif command == kCommand_SetThermostatMode:
					theUrl = "http://%s:%i/%s&%s" % (self.address, self.port, kActionUrl, kThermostatServiceString_Mode % (deviceId, commandDict["value"]))
					self.logMethod("_processCommand: url: %s" % theUrl)
					self._executeUrl(theUrl, deviceName, "set mode to %s" % commandDict["value"])
				elif command == kCommand_SetThermostatFanMode:
					theUrl = "http://%s:%i/%s&%s" % (self.address, self.port, kActionUrl, kThermostatServiceString_FanMode % (deviceId, commandDict["value"]))
					self.logMethod("_processCommand: url: %s" % theUrl)
					self._executeUrl(theUrl, deviceName, "set mode to %s" % commandDict["value"])

				elif command == kCommand_Unlock:
					theUrl = "http://%s:%i/%s&%s" % (self.address, self.port, kActionUrl, kLockServiceString % (deviceId, 0))
					self._executeUrl(theUrl, deviceName, "unlock")
				elif command == kCommand_Lock:
					theUrl = "http://%s:%i/%s&%s" % (self.address, self.port, kActionUrl, kLockServiceString % (deviceId, 1))
					self._executeUrl(theUrl, deviceName, "lock")
#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################

import traceback
from datetime import datetime, time
from operator import itemgetter
import re
import Queue
import veralib

################################################################################
# Globals
################################################################################
kPort = u"3480"
kFailCountTrigger = 60 * 15
kThermostatModeLookup = {
	"Off"				:	indigo.kHvacMode.Off,
	"CoolOn"			:	indigo.kHvacMode.Cool,
	"HeatOn"			:	indigo.kHvacMode.Heat,
	"AutoChangeOver"	:	indigo.kHvacMode.HeatCool,
}

kThermostatFanLookup = {
	"Auto"			:	indigo.kFanMode.Auto,
	"ContinuousOn"	:	indigo.kFanMode.AlwaysOn,
	"PeriodicOn"	:	indigo.kFanMode.Auto,		# supports a periodic cycle but we don't
}


################################################################################
def isValidHostname(hostname):
	if len(hostname) > 255 or len(hostname) < 1:
		return False
	if hostname[-1] == ".":
		hostname = hostname[:-1] # strip exactly one dot from the right, if present
	allowed = re.compile("(?!-)[A-Z\d-]{1,63}(?<!-)$", re.IGNORECASE)
	return all(allowed.match(x) for x in hostname.split("."))

################################################################################
class Plugin(indigo.PluginBase):
	########################################
	def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs): 
		super(Plugin, self).__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
		self.debug = pluginPrefs.get("showDebugInfo", False)
		self.host = pluginPrefs.get("host", None)
		self.port = int(pluginPrefs.get("port", kPort))
		self.vera = None
		self.restartVera = False
		self.deviceDict = {}
		if self.host == "localhost" or self.host == "127.0.0.1":
			self.demoMode = True
		else:
			self.demoMode = False

	########################################
	def _getNodeList(self, filter="", valuesDict=None, typeId="", targetId=0):
		self.debugLog("_getNodeList called")
		returnTup = []
		if self.vera:
			if filter == "devices":
				curDeviceNum = 0
				if targetId:
					if targetId in indigo.devices:
						addStr = indigo.devices[targetId].address
						if addStr:
							curDeviceNum = int(addStr)
				veraDeviceNumList = []
				# if the device is being edited the device should show up in the list selected
				for device in indigo.devices.iter("com.perceptiveautomation.indigoplugin.vera"):
					if device.configured and int(device.address) != curDeviceNum:
						veraDeviceNumList.append(int(device.address))
				for id, deviceDict in self.vera.devices.items():
					veraDeviceId = valuesDict.get("veraDeviceId", 0)
					if veraDeviceId == "":
						veraDeviceId = 0
					if id not in veraDeviceNumList or id == int(veraDeviceId):
						returnTup.append((id, deviceDict["name"]))
			else:
				# looking for scenes - this one's easy
				returnTup = [(key, item.get("name", "Unknown")) for key, item in self.vera.scenes.items()]
		return sorted(returnTup, key=itemgetter(1))
		
	########################################
	def refreshNodeList(self, valuesDict, typeId="", devId=None):
		# No need to actually do anything here, we just need _getNodeList called
		# again
		pass

	########################################
	def getDeviceFactoryUiValues(self, devIdList):
		self.debugLog("getDeviceFactoryUiValues: %s" % str(devIdList))
		isInitialDefine = (len(devIdList) == 0)
		valuesDict = indigo.Dict()
		if not isInitialDefine and devIdList[0] in indigo.devices:
			dev = indigo.devices[devIdList[0]]
			nodeId = dev.address
			if nodeId != "":
				valuesDict['veraDeviceId'] = nodeId
		errorsDict = indigo.Dict()
		return (valuesDict, errorsDict)
		
	########################################
	def validateDeviceFactoryUi(self, valuesDict, devIdList):
		errorsDict = indigo.Dict()
		address = valuesDict.get("veraDeviceId", None)
		if not address or address == "":
			errorsDict["veraDeviceId"] = u"You must select a device"
		if len(errorsDict):
			return (False, valuesDict, errorsDict)
		return (True, valuesDict)
		
	########################################
	def closedDeviceFactoryUi(self, valuesDict, userCancelled, devIdList):
		self.debugLog("closedDeviceFactoryUi")
		if not userCancelled:
			deviceTypeMap = None
			if len(devIdList) > 0:
				self.debugLog("closedDeviceFactoryUi: devIdList: %s" % str(devIdList))
				dev = indigo.devices[devIdList[0]]
				if dev:
					deviceDict = self.vera.devices[int(valuesDict["veraDeviceId"])]
					deviceTypeMap = veralib.modelForDeviceInfo(deviceDict)
					dev.model = deviceTypeMap[1]
					dev.replaceOnServer()
					props = dev.pluginProps
 					if "watts" not in deviceDict and "SupportsEnergyMeter" in props:
 						del props["SupportsEnergyMeter"]
						del props["SupportsEnergyMeterCurPower"]
					if "batterylevel" in deviceDict:
						props["SupportsBatteryLevel"] = True
					props["address"] = valuesDict["veraDeviceId"]
					dev.replacePluginPropsOnServer(props)
					indigo.device.changeDeviceTypeId(dev, deviceTypeMap[0])
			else:
				# this is the first time the device has been created
				deviceDict = self.vera.devices[int(valuesDict["veraDeviceId"])]
				deviceTypeMap = veralib.modelForDeviceInfo(deviceDict)
				self.debugLog("closedDeviceFactoryUi: creating device for: %s" % str(deviceDict))
				newProps = indigo.Dict()
				if "watts" in deviceDict:
					self.debugLog("closedDeviceFactoryUi: entered watts block")
 					newProps["SupportsEnergyMeterCurPower"] = True
 					newProps["SupportsEnergyMeter"] = True
				if "batterylevel" in deviceDict:
					newProps["SupportsBatteryLevel"] = True
				if deviceTypeMap:
					dev = indigo.device.create(
						protocol=indigo.kProtocol.Plugin,
						address=str(valuesDict["veraDeviceId"]),
						deviceTypeId=deviceTypeMap[0],
						props=newProps,
						name=self.getUniqueDeviceName(deviceDict["name"]))
					self.debugLog("closedDeviceFactoryUi: finished device for: %s" % str(dev))

	########################################
	def validatePrefsConfigUi(self, valuesDict):
		errorsDict = indigo.Dict()
		if "host" not in valuesDict:
			errorsDict["host"] = 'You must specify a host name or IP address for your Vera.'
		else:
			host = valuesDict["host"]
			if not isValidHostname(valuesDict["host"]):
				errorsDict["host"] = 'You must specify a valid host name or IP address for your Vera.'
		if "port" not in valuesDict:
			errorsDict["host"] = 'You must specify a port number for your Vera. "%s" is the default port number for the Vera.' % kPort
		else:
			port = valuesDict["port"]
			try:
				portNumber = int(port)
				if portNumber > 65535 or portNumber < 1:
					errorsDict["port"] ="Invalid port number specified"
			except:
				errorsDict["port"] ="Invalid port number specified"
		if len(errorsDict) > 0:
			return (False, valuesDict, errorsDict)
		else:
			self.host = host
			self.port = portNumber
			if host == "127.0.0.1" or host == "localhost":
				# We want to run the plugin in demo mode - never try to do any comm but just set
				# device values directly.
				self.demoMode = True
				return (True, valuesDict)
			try:
				if self.vera and self.vera.isAlive():
					self.vera.stop()
				self.sleep(5)
				self.debugLog("trying to create vera thread...")
				self.vera = veralib.Vera(self.host, self.port, indigo.server.log, self.debugLog)
				self.vera.threadDebug = valuesDict.get("threadDebug", False)
				self.restartVera = True
				self.debugLog("validatePrefsConfigUi: valuesDict: %s" % str(valuesDict))
				return (True, valuesDict)
			except Exception, e:
				errorsDict["host"] = "Host or port are not valid or the Vera is unreachable."
				errorsDict["port"] = "Host or port are not valid or the Vera is unreachable."
				self.debugLog("validatePrefsConfigUi: can't start vera thread: %s" % traceback.format_exc(10))
				return (False, valuesDict, errorsDict)
			
	########################################
	def deviceStartComm(self, dev):
		self.debugLog("deviceStartComm called with: device.address: %s" % dev.address)
		if self.vera:
			self.vera.doFullUpdate()
		if dev.id not in self.deviceDict:
			if dev.configured:
				self.deviceDict[dev.address] = dev.id
				self.debugLog(dev.name + " communication enabled")
			else:
				indigo.device.enable(dev, value=False) 
				self.errorLog(dev.name + " automatically disabled as no device type is set (see device configuration)")
				return
		self.debugLog("deviceStartComm: self.deviceDict: %s" % str(self.deviceDict))

	########################################
	def deviceStopComm(self, dev):
		self.debugLog("deviceStopComm called with: device.address: %s" % dev.address)
		if dev.address in self.deviceDict:
			del self.deviceDict[dev.address]
			if self.debug:
				indigo.server.log(dev.name + " communication disabled")

	########################################
	def runConcurrentThread(self):
		self.debugLog("Starting concurrent tread")
		try:
			while True:
				if not self.host:
					self.sleep(3)
					continue
				failCount = 0
				while not self.vera and not self.demoMode:
					try:
						self.vera = veralib.Vera(self.host, self.port, indigo.server.log, self.debugLog)
						self.restartVera = True
						self.vera.threadDebug = self.pluginPrefs.get("threadDebug", False)
					except self.StopThread:
						raise self.StopThread
					except:
						if failCount < 1 or (failCount >= kFailCountTrigger):
							self.errorLog("Can't communicate with the Vera - make sure the plugin settings are correct and that the Vera is running and accessible.")
							self.errorLog("Will continue to retry every 15 seconds silently.")
							if failCount >= kFailCountTrigger:
								failCount = 0
						failCount += 1
						self.sleep(15)
				if self.vera and self.restartVera and not self.demoMode:
					errorNeedsDisplay = True
					while not self.vera.isAlive():
						try:
							self.vera.start()
							self.debugLog("runConcurrentThread: started thread")
							self.vera.commandQueue = Queue.Queue()
							self.vera.doFullUpdate()
							self.restartVera = False
						except self.StopThread:
							self.debugLog("runConcurrentThread: vera start loop got StopThread")
							raise self.StopThread
						except Exception, e:
							self.debugLog("Vera thread can't start, will continue to retry: %s" % str(e))
							if errorNeedsDisplay:
								self.errorLog("Vera thread can't start, will continue to retry silently every 15 seconds: %s" % str(e))
								loggedError = True
							self.sleep(15)
				if self.vera and self.vera.isAlive() and not self.demoMode:
					queueHasItems = True
					while self.vera and self.vera.updateQueue and queueHasItems:
						try:
							updateDict = self.vera.updateQueue.get_nowait()
							self.debugLog("runConcurrentThread: processing update: %s" % str(updateDict))
							self.processUpdate(updateDict)
							self.commandQueue.task_done()
						except self.StopThread:
							raise self.StopThread
						except:
							queueHasItems = False
					self.sleep(3)
				elif self.demoMode:
					self.sleep(3)
		except self.StopThread:
			if self.vera and self.vera.isAlive() and not self.demoMode:
				self.vera.stop()

	########################################
	def processUpdate(self, updateDict):
		self.debugLog("processUpdate called")
		updateType = updateDict["updateType"]
		if updateType == "updateDevice":
			#update the states
			deviceInfo = updateDict["device"]
			devAddress = deviceInfo.get("id", -1)
			devId = self.deviceDict.get(str(devAddress), 0)
			dev = indigo.devices.get(devId, None)
			keyValueList = []
			if dev and dev.enabled:
				try:
					self.debugLog("processUpdate start: found device (%s) updating: %s" % (dev.name, deviceInfo))
					# This first set of if/elif will take care of dimmers, locks, and relays.
					if "level" in deviceInfo:
						keyValueList.append({'key':'brightnessLevel', 'value':deviceInfo["level"]})
					elif "locked" in deviceInfo:
						keyValueList.append({'key':'onOffState', 'value':bool(int(deviceInfo["locked"]))})
					elif "status" in deviceInfo and dev.deviceTypeId != "veraThermostat":  #some versions of the API send an erroneous status for thermostats which have no on/off state
						keyValueList.append({'key':'onOffState', 'value':bool(int(deviceInfo["status"]))})	

					# Next, we deal with thermostat and other values
					if u'mode' in deviceInfo:
						keyValueList.append({'key':'hvacOperationMode', 'value':kThermostatModeLookup[deviceInfo["mode"]]})
					if "heatsp" in deviceInfo:
						keyValueList.append({'key':'setpointHeat', 'value':int(deviceInfo["heatsp"])})
					if "coolsp" in deviceInfo:
						keyValueList.append({'key':'setpointCool', 'value':int(deviceInfo["coolsp"])})
					if "temperature" in deviceInfo:
						keyValueList.append({'key':'temperatureInput1', 'value':int(deviceInfo["temperature"])})
					if "fanmode" in deviceInfo:
						keyValueList.append({'key':'hvacFanMode', 'value':kThermostatFanLookup[deviceInfo["fanmode"]]})
					if "batterylevel" in deviceInfo:
						uiString = "%s%%" % deviceInfo["batterylevel"]
						keyValueList.append({'key':'batteryLevel', 'value':int(deviceInfo["batterylevel"]), 'uiValue':uiString})
					if "watts" in deviceInfo:
						uiString = ("%2.2f W" % float(deviceInfo["watts"]))
						keyValueList.append({'key':'curEnergyLevel', 'value':deviceInfo["watts"], 'uiValue':uiString})
					if "kwh" in deviceInfo:
						uiString = ("%2.3f kWh" % float(deviceInfo["kwh"]))
						keyValueList.append({'key':'accumEnergyTotal', 'value':deviceInfo["kwh"], 'uiValue':uiString})

					# Now we can process keyValueList and update all the device states
					if len(keyValueList) > 0:
						dev.updateStatesOnServer(keyValueList)

					# And, finaly, check to see if the device is in an error state
					if "state" in deviceInfo:
						if deviceInfo["state"] in veralib.kErrorStates:
							dev.setErrorStateOnServer("device error")

					self.debugLog("processUpdate Finished: for device (%s)  : %s" % (dev.name, deviceInfo))

				except Exception, e:
					self.logger.exception(u"Error encountered in processUpdate")
					return
			else:
				if dev:
					self.debugLog("processUpdate: device with Vera ID %i found (%s) but is disabled, skipping update" % (devAddress, dev.name))
				else:
					self.debugLog("processUpdate: no device with Vera ID %i found, skipping update" % devAddress)
		elif updateType == "deleteDevice":
			self.debugLog("\n\nDELETING DEVICE\n\n")
			# the device disappeared from the vera so we'll want to deal with it
			devAddress = updateDict.get("device", -1)
			self.debugLog("deleting device id: %s" % str(devAddress))
			devId = self.deviceDict.get(str(devAddress), 0)
			dev = indigo.devices.get(devId, None)
			if dev:
				dev.setErrorStateOnServer("device deleted")
				self.errorLog('Device "%s" (id: %s) deleted on the Vera' % (dev.name, devAddress))

	########################################
	def getUniqueDeviceName(self, seedName):
		seedName = seedName.strip()
		if (seedName not in indigo.devices):
			return seedName
		else:
			counter = 1
			candidate = seedName + " " + str(counter)
			while candidate in indigo.devices:
				counter = counter + 1
				candidate = seedName + " " + str(counter)
			return candidate

	########################################
	# Action Methods
	########################################
	# General Action callbacks first
	######################
	def actionControlUniversal(self, action, dev):
		###### BEEP ######
		if action.deviceAction == indigo.kUniversalAction.Beep:
			# Beep the hardware module (dev) here:
			# This is dumy code 9n case someday it is needed
			indigo.server.log(u"sent \"%s\" %s" % (dev.name, "beep request"))

		###### ENERGY UPDATE ######
		elif action.deviceAction == indigo.kUniversalAction.EnergyUpdate:
			self.debugLog(u"received request for \"%s\" %s" % (dev.name, action))
			# dev=indigo.devices[action.deviceId] # "Bergerie Patio Light"
			self.debugLog(u"found device \"%s %s" % (dev.name, dev.address))
			# Request hardware module (dev) for its most recent meter data here:
			self.vera._update(fullUpdate=False, updateDevAddress=dev.address)

			#self._refreshStatesFromHardware(dev, True)

		###### ENERGY RESET ######
		elif action.deviceAction == indigo.kUniversalAction.EnergyReset:
			# Request that the hardware module (dev) reset its accumulative energy usage data here:
			indigo.server.log(u"received request for \"%s\" %s" % (dev.name, "energy usage reset"))
			# Just ell Indigo to reset it by setting the value to 0.
			# This will automatically reset Indigo's time stamp for the accumulation.
			self.vera._kwhReset(dev.address)
			dev.updateStateOnServer("accumEnergyTotal", 0.0)


		###### STATUS REQUEST ######
		elif action.deviceAction == indigo.kUniversalAction.RequestStatus:
			indigo.server.log(u"received request for \"%s\" %s" % (dev.name, "status request"))
			# Query hardware module (dev) for its current status here:
			# Another placeholder for the future
			# self._refreshStatesFromHardware(dev, True)

	def actionControlDimmerRelay(self, action, dev):
		if (self.vera and self.vera.isAlive() and self.vera.commandQueue and dev.enabled) or self.demoMode:
			if dev.deviceTypeId == "veraLock":
				if action.deviceAction == indigo.kDeviceAction.TurnOff:
					if self.demoMode:
						self.sleep(1.5)
						dev.updateStateOnServer(key="onOffState", value=False)
					else:
						self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_Unlock})
				elif action.deviceAction == indigo.kDeviceAction.TurnOn:
					if self.demoMode:
						self.sleep(1.5)
						dev.updateStateOnServer(key="onOffState", value=True)
					else:
						self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_Lock})
				elif action.deviceAction == indigo.kDeviceAction.Toggle:
					if self.demoMode:
						self.sleep(1.5)
						dev.updateStateOnServer(key="onOffState", value=not dev.onState)
					else:
						if dev.onState:
							self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_Unlock})
						else:
							self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_Lock})
			else:
				if self.demoMode:
						self.errorLog("Only lock devices are supported in demo mode.")
						return
				if action.deviceAction == indigo.kDeviceAction.TurnOff:
					self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_TurnOff})
				elif action.deviceAction == indigo.kDeviceAction.TurnOn:
					self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_TurnOn})
				elif action.deviceAction == indigo.kDeviceAction.Toggle:
					if dev.onState:
						self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_TurnOff})
					else:
						self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_TurnOn})
				elif action.deviceAction == indigo.kDeviceAction.SetBrightness:
					self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_SetBrightness, "value":action.actionValue})
				elif action.deviceAction == indigo.kDeviceAction.BrightenBy:
					newBrightness = dev.brightness + action.actionValue
					if newBrightness == 0:
						newBrightness = action.actionValue
					if newBrightness > 100:
						newBrightness = 100
					self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_SetBrightness, "value":newBrightness})
				elif action.deviceAction == indigo.kDeviceAction.DimBy:
					newBrightness = dev.brightness - action.actionValue
					if newBrightness < 0:
						newBrightness = 0
					self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_SetBrightness, "value":newBrightness})
		else:
			self.errorLog("Command not sent - either the device is disabled or the vera communication thread isn't running.")
						
	def actionControlThermostat(self, action, dev):
		if self.demoMode:
			self.errorLog("Only lock devices are supported in demo mode.")
			return
		if self.vera and self.vera.isAlive() and self.vera.commandQueue and dev.enabled:
			self.debugLog("actionControlThermostat: device id: %s, action: %s" % (dev.address, str(action.thermostatAction)))
			###### SET HVAC MODE ######
			if action.thermostatAction == indigo.kThermostatAction.SetHvacMode:
				id = int(dev.address)
				if action.actionMode == indigo.kHvacMode.Off:
					command = veralib.kThermostatModes["Off"]
				elif action.actionMode == indigo.kHvacMode.Heat or action.actionMode == indigo.kHvacMode.ProgramHeat:
					command = veralib.kThermostatModes["Heat"]				
				elif action.actionMode == indigo.kHvacMode.Cool or action.actionMode == indigo.kHvacMode.ProgramCool:
					command = veralib.kThermostatModes["Cool"]				
				elif action.actionMode == indigo.kHvacMode.HeatCool or action.actionMode == indigo.kHvacMode.ProgramHeatCool:
					command = veralib.kThermostatModes["HeatCool"]
				else:
					self.errorLog("actionControlThermostat: Set HVAC mode action has an invalid action mode")
					return
				self.debugLog("actionControlThermostat: set havc mode vera command: %s" % command) 
				self.vera.commandQueue.put_nowait({"id": id, "command":veralib.kCommand_SetThermostatMode, "value":command})

			###### SET FAN MODE ######
			elif action.thermostatAction == indigo.kThermostatAction.SetFanMode:
				if action.actionMode == indigo.kFanMode.Auto:
					command = veralib.kThermostatFanModes["Auto"]
				elif action.actionMode == indigo.kFanMode.AlwaysOn:
					command = veralib.kThermostatFanModes["AlwaysOn"]				
				else:
					self.errorLog("actionControlThermostat: Set fan mode action has an invalid action mode")
					return
				self.debugLog("actionControlThermostat: set fan mode vera command: %s" % command) 
				self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_SetThermostatFanMode, "value":command})

			###### SET COOL SETPOINT ######
			elif action.thermostatAction == indigo.kThermostatAction.SetCoolSetpoint:
				self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_SetCoolSetpoint, "value":action.actionValue})

			###### SET HEAT SETPOINT ######
			elif action.thermostatAction == indigo.kThermostatAction.SetHeatSetpoint:
				self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_SetHeatSetpoint, "value":action.actionValue})

			###### DECREASE/INCREASE COOL SETPOINT ######
			elif action.thermostatAction == indigo.kThermostatAction.DecreaseCoolSetpoint:
				newSetpoint = dev.coolSetpoint - action.actionValue
				self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_SetCoolSetpoint, "value":int(newSetpoint)})

			elif action.thermostatAction == indigo.kThermostatAction.IncreaseCoolSetpoint:
				newSetpoint = dev.coolSetpoint + action.actionValue
				self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_SetCoolSetpoint, "value":int(newSetpoint)})

			###### DECREASE/INCREASE HEAT SETPOINT ######
			elif action.thermostatAction == indigo.kThermostatAction.DecreaseHeatSetpoint:
				newSetpoint = dev.heatSetpoint - action.actionValue
				self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_SetHeatSetpoint, "value":int(newSetpoint)})

			elif action.thermostatAction == indigo.kThermostatAction.IncreaseHeatSetpoint:
				newSetpoint = dev.heatSetpoint + action.actionValue
				self.vera.commandQueue.put_nowait({"id": int(dev.address), "command":veralib.kCommand_SetHeatSetpoint, "value":int(newSetpoint)})
		else:
			self.errorLog("Command not sent - either the device is disabled or the vera communication thread isn't running.")

	########################################
	def actionControlGeneral(self, action, dev):
		if action.deviceAction == indigo.kDeviceGeneralAction.RequestStatus:
			if self.vera:
				self.vera.doFullUpdate()
			indigo.server.log(u"sent full update request - all devices will be refreshed in the next update")
			
	########################################
	def runScene(self, action):
		if self.demoMode:
			self.errorLog("Scene control not available in demo mode.")
			return
		# add the command to the vera queue
		sceneId = action.props.get("sceneId", None)
		if sceneId:
			self.vera.commandQueue.put_nowait({"id": int(sceneId), "command":veralib.kCommand_RunScene})

	########################################
	# Menu Methods
	########################################
	def toggleDebugging(self):
		if self.demoMode:
			self.errorLog("Debug mode not available in demo mode.")
			return
		if self.debug:
			indigo.server.log("Turning off debug logging")
			self.pluginPrefs["showDebugInfo"] = False
		else:
			indigo.server.log("Turning on debug logging")
			self.pluginPrefs["showDebugInfo"] = True
		self.debug = not self.debug

	def updateAll(self):
		if self.demoMode:
			self.errorLog("Update all not available in demo mode.")
			return
		indigo.server.log("Starting update all")
		self.vera._update(fullUpdate=True,)
		


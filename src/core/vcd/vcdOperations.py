# ***************************************************
# Copyright © 2020 VMware, Inc. All rights reserved.
# ***************************************************

"""
Description: Module which performs the VMware Cloud Director NSX-V to NSX-T Migration Operations
"""

import ipaddress
import logging
import json
import re
import time
import os
import copy

import requests
import xmltodict

from src.commonUtils.utils import Utilities
import src.core.vcd.vcdConstants as vcdConstants

from src.core.vcd.vcdValidations import isSessionExpired, description, remediate
from src.core.vcd.vcdConfigureEdgeGatewayServices import ConfigureEdgeGatewayServices
logger = logging.getLogger('mainLogger')


class VCloudDirectorOperations(ConfigureEdgeGatewayServices):
    """
    Description: Class that performs the VMware Cloud Director NSX-V to NSX-T Migration Operations
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        vcdConstants.VCD_API_HEADER = vcdConstants.VCD_API_HEADER.format(self.version)
        vcdConstants.GENERAL_JSON_CONTENT_TYPE = vcdConstants.GENERAL_JSON_CONTENT_TYPE.format(self.version)

    @description("creation of target Org VDC Edge Gateway")
    @remediate
    def createEdgeGateway(self, vcdDict, nsxObj):
        """
        Description :   Creates an Edge Gateway in the specified Organization VDC
        """
        try:
            logger.info('Creating target Org VDC Edge Gateway')
            # reading data from apiOutput.json
            data = self.rollback.apiData

            logger.debug("Updating Target External network {} with sub allocated ip pools".format(vcdDict.NSXTProviderVDCExternalNetwork))
            # getting details of ip ranges used in source edge gateways
            edgeGatewaySubnetDict = {}
            for edgeGateway in copy.deepcopy(data['sourceEdgeGateway']):
                for edgeGatewayUplink in edgeGateway['edgeGatewayUplinks']:
                    for subnet in edgeGatewayUplink['subnets']['values']:
                        # Getting value of primary ip
                        primaryIp = subnet.get('primaryIp')
                        # Creating ip range for primary ip
                        subIpRange = [{'startAddress': primaryIp, 'endAddress': primaryIp}]
                        networkAddress = ipaddress.ip_network('{}/{}'.format(subnet['gateway'], subnet['prefixLength']),
                                                              strict=False)
                        if networkAddress in edgeGatewaySubnetDict:
                            edgeGatewaySubnetDict[networkAddress].extend(subnet['ipRanges']['values'])
                        else:
                            edgeGatewaySubnetDict[networkAddress] = subnet['ipRanges']['values']

                        # adding primary ip to sub alloacated ip pool
                        if primaryIp and ipaddress.ip_address(primaryIp) in networkAddress:
                            edgeGatewaySubnetDict[networkAddress].extend(subIpRange)

            # Getting target external network details
            externalDict = self.getExternalNetwork(vcdDict.NSXTProviderVDCExternalNetwork)
            external_network_id = externalDict['id']
            for index, subnet in enumerate(externalDict['subnets']['values']):
                subnetOfTargetExtNetToUpdate = ipaddress.ip_network(
                    '{}/{}'.format(subnet['gateway'], subnet['prefixLength']), strict=False)
                # if no ip present to update of corrensponding subnet skip the operation
                if not edgeGatewaySubnetDict.get(subnetOfTargetExtNetToUpdate):
                    continue
                externalDict['subnets']['values'][index]['ipRanges']['values'].extend(
                    edgeGatewaySubnetDict.get(subnetOfTargetExtNetToUpdate))

            # url to update external network properties
            url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                   vcdConstants.ALL_EXTERNAL_NETWORKS, external_network_id)
            # put api call to update external netowork
            self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
            payloadData = json.dumps(externalDict)
            response = self.restClientObj.put(url, self.headers, data=payloadData)
            if response.status_code == requests.codes.accepted:
                taskUrl = response.headers['Location']
                # checking the status of the updating external network task
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug('Target External network {} updated successfully with sub allocated ip pools.'.format(
                    externalDict['name']))
                self.isExternalNetworkUpdated = True
            else:
                errorResponse = response.json()
                raise Exception('Failed to update External network {} with sub allocated ip pools - {}'.format(
                    externalDict['name'], errorResponse['message']))

            # getting the edge gateway details of the target org vdc
            targetEdgeGatewayNames = [edgeGateway['name'] for edgeGateway in
                                      self.getOrgVDCEdgeGateway(data['targetOrgVDC']['@id'])['values']]

            for sourceEdgeGatewayDict in copy.deepcopy(data['sourceEdgeGateway']):
                if sourceEdgeGatewayDict['name'] in targetEdgeGatewayNames:
                    continue

                # Checking if default edge gateway is configured on edge gateway
                sourceEdgeGatewayId = sourceEdgeGatewayDict['id'].split(':')[-1]
                defaultGatewayData = self.getEdgeGatewayAdminApiDetails(sourceEdgeGatewayId, returnDefaultGateway=True)
                if isinstance(defaultGatewayData, list):
                    raise Exception(
                        'Default gateway is not configured on edge gateway - {}'.format(sourceEdgeGatewayDict['name']))
                defaultGateway = defaultGatewayData.get('gateway')

                bgpConfigDict = self.getEdgegatewayBGPconfig(sourceEdgeGatewayId, validation=False)
                data = self.rollback.apiData
                externalDict = self.getExternalNetwork(vcdDict.NSXTProviderVDCExternalNetwork)
                # edge gateway create URL
                url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EDGE_GATEWAYS)
                filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.json')
                # creating payload dictionary
                payloadDict = {'edgeGatewayName': sourceEdgeGatewayDict['name'],
                               'edgeGatewayDescription': sourceEdgeGatewayDict[
                                   'description'] if sourceEdgeGatewayDict.get('description') else '',
                               'orgVDCName': data['targetOrgVDC']['@name'],
                               'orgVDCId': data['targetOrgVDC']['@id'],
                               'orgName': data['Organization']['@name'],
                               'orgId': data['Organization']['@id'],
                               'externalNetworkId': externalDict['id'],
                               'externalNetworkName': externalDict['name'],
                               'nsxtManagerName': externalDict['networkBackings']['values'][0]['networkProvider'][
                                   'name'],
                               'nsxtManagerId': externalDict['networkBackings']['values'][0]['networkProvider']['id']
                               }

                if (isinstance(bgpConfigDict, tuple) and not bgpConfigDict[0]) or not bgpConfigDict or bgpConfigDict['enabled'] != "true":
                    payloadDict['dedicated'] = False
                else:
                    payloadDict['dedicated'] = True
                # creating payload data
                payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='json',
                                                          componentName=vcdConstants.COMPONENT_NAME,
                                                          templateName=vcdConstants.CREATE_ORG_VDC_EDGE_GATEWAY_TEMPLATE)

                # adding sub allocated ip pool in edge gateway payload
                subnetData = []

                # Adding primary ip in sub - allocated pool of egde gateway
                for uplink in sourceEdgeGatewayDict['edgeGatewayUplinks']:
                    for subnet in uplink['subnets']['values']:
                        # Getting value of primary ip
                        primaryIp = subnet.get('primaryIp')
                        # Creating ip range for primary ip
                        subIpRange = [{'startAddress': primaryIp, 'endAddress': primaryIp}]

                        networkAddress = ipaddress.ip_network('{}/{}'.format(subnet['gateway'], subnet['prefixLength']),
                                                              strict=False)
                        # adding primary ip to sub alloacated ip pool
                        if primaryIp and ipaddress.ip_address(primaryIp) in networkAddress:
                            subnet['ipRanges']['values'].extend(subIpRange)

                    subnetData += uplink['subnets']['values']

                # Setting primary ip to be used for edge gateway creation
                for subnet in subnetData:
                    if subnet['gateway'] == defaultGateway:
                        continue
                    else:
                        subnet['primaryIp'] = None

                payloadData = json.loads(payloadData)
                payloadData['edgeGatewayUplinks'][0]['subnets'] = {}
                payloadData['edgeGatewayUplinks'][0]['subnets']['values'] = subnetData

                # Checking if edge cluster is specified in user input yaml
                if getattr(vcdDict, 'EdgeGatewayDeploymentEdgeCluster', None):
                    # Fetch edge cluster id
                    edgeClusterId = nsxObj.fetchEdgeClusterDetails(vcdDict.EdgeGatewayDeploymentEdgeCluster).get('id')
                else:
                    tier0RouterName = \
                    data['targetExternalNetwork']['networkBackings']['values'][0]['name']
                    edgeClusterId = nsxObj.fetchEdgeClusterIdForTier0Gateway(tier0RouterName)

                # Create edge cluster config payload for edge gateway
                edgeClusterConfigPayload = {"edgeClusterConfig":
                                                {"primaryEdgeCluster":
                                                     {"backingId": edgeClusterId}}}
                # Updating edge cluster configuration payload in edge gateway creation payload
                payloadData.update(edgeClusterConfigPayload)

                payloadData = json.dumps(payloadData)

                self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                # post api to create edge gateway
                response = self.restClientObj.post(url, self.headers, data=payloadData)
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    # checking the status of creating target edge gateway task
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug('Target Edge Gateway created successfully.')
                else:
                    errorResponse = response.json()
                    raise Exception(
                        'Failed to create target Org VDC Edge Gateway - {}'.format(errorResponse['message']))
            # getting the edge gateway details of the target org vdc
            responseDict = self.getOrgVDCEdgeGateway(data['targetOrgVDC']['@id'])
            data['targetEdgeGateway'] = responseDict['values']
            return [value['id'] for value in responseDict['values']]
        except Exception:
            raise

    @description("creation of target Org VDC Networks")
    @remediate
    def createOrgVDCNetwork(self, sourceOrgVDCNetworks):
        """
        Description : Create Org VDC Networks in the specified Organization VDC
        """
        try:
            logger.info('Creating target Org VDC Networks')
            data = self.rollback.apiData
            targetOrgVDC = data['targetOrgVDC']
            targetEdgeGateway = data['targetEdgeGateway']
            # org vdc network create URL
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_ORG_VDC_NETWORKS)
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.json')
            for sourceOrgVDCNetwork in sourceOrgVDCNetworks:
                # creating payload dictionary
                payloadDict = {'orgVDCNetworkName': sourceOrgVDCNetwork['name'] + '-v2t',
                               'orgVDCNetworkDescription': sourceOrgVDCNetwork[
                                   'description'] if sourceOrgVDCNetwork.get('description') else '',
                               'orgVDCNetworkGateway': sourceOrgVDCNetwork['subnets']['values'][0]['gateway'],
                               'orgVDCNetworkPrefixLength': sourceOrgVDCNetwork['subnets']['values'][0]['prefixLength'],
                               'orgVDCNetworkDNSSuffix': sourceOrgVDCNetwork['subnets']['values'][0]['dnsSuffix'],
                               'orgVDCNetworkDNSServer1': sourceOrgVDCNetwork['subnets']['values'][0]['dnsServer1'],
                               'orgVDCNetworkDNSServer2': sourceOrgVDCNetwork['subnets']['values'][0]['dnsServer2'],

                               'orgVDCNetworkType': sourceOrgVDCNetwork['networkType'],
                               'orgVDCName': targetOrgVDC['@name'],
                               'orgVDCId': targetOrgVDC['@id']}
                if sourceOrgVDCNetwork['networkType'] == "ISOLATED":
                    payloadDict.update({'edgeGatewayName': "", 'edgeGatewayId': "", 'edgeGatewayConnectionType': ""})
                else:
                    edgeGatewayName = sourceOrgVDCNetwork['connection']['routerRef']['name']
                    edgeGatewayId = \
                    list(filter(lambda edgeGatewayData: edgeGatewayData['name'] == edgeGatewayName, targetEdgeGateway))[
                        0]['id']
                    payloadDict.update({'edgeGatewayName': edgeGatewayName,
                                        'edgeGatewayId': edgeGatewayId})
                # creating payload data
                payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='json',
                                                          componentName=vcdConstants.COMPONENT_NAME,
                                                          templateName=vcdConstants.CREATE_ORG_VDC_NETWORK_TEMPLATE, apiVersion=self.version)
                if sourceOrgVDCNetwork['networkType'] == "ISOLATED":
                    payloadData = json.loads(payloadData)
                    payloadData['connection'] = {}
                    payloadData = json.dumps(payloadData)
                if not sourceOrgVDCNetwork['subnets']['values'][0]['ipRanges']['values']:
                    payloadData = json.loads(payloadData)
                    payloadData['subnets']['values'][0]['ipRanges']['values'] = None
                    payloadData = json.dumps(payloadData)
                else:
                    ipRangeList = []
                    for ipRange in sourceOrgVDCNetwork['subnets']['values'][0]['ipRanges']['values']:
                        ipPoolDict = {}
                        ipPoolDict['startAddress'] = ipRange['startAddress']
                        ipPoolDict['endAddress'] = ipRange['endAddress']
                        ipRangeList.append(ipPoolDict)
                    payloadData = json.loads(payloadData)
                    payloadData['subnets']['values'][0]['ipRanges']['values'] = ipRangeList
                    payloadData = json.dumps(payloadData)
                self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                # post api to create org vdc network
                response = self.restClientObj.post(url, self.headers, data=payloadData)
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    # checking the status of the creating org vdc network task
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug('Target Org VDC Network {} created successfully.'.format(sourceOrgVDCNetwork['name']))
                else:
                    errorResponse = response.json()
                    raise Exception(
                        'Failed to create target Org VDC Network {} - {}'.format(sourceOrgVDCNetwork['name'],
                                                                                 errorResponse['message']))
            # saving the org vdc network details to apiOutput.json
            self.getOrgVDCNetworks(targetOrgVDC['@id'], 'targetOrgVDCNetworks', saveResponse=True)
            logger.info('Successfully created target Org VDC Networks.')
            conflictNetwork = self.rollback.apiData.get('ConflictNetworks')
            if conflictNetwork:
                networkList = list()
                targetnetworks = self.retrieveNetworkListFromMetadata(targetOrgVDC['@id'], dfwStatus=False, orgVDCType='target')
                for targetnetwork in targetnetworks:
                    for network in conflictNetwork:
                        if network['name'] + '-v2t' == targetnetwork['name']:
                            # networkIds = list(filter(lambda network: network['name']+'-v2t' == targetnetwork['name'], conflictNetwork))[0]
                            networkList.append({'name': network['name'], 'id': targetnetwork['id']})
                self.rollback.apiData['ConflictNetworks'] = networkList
        except:
            raise

    @isSessionExpired
    def deleteOrgVDC(self, orgVDCId, rollback=False):
        """
        Description :   Deletes the specified Organization VDC
        Parameters  :   orgVDCId  -   Id of the Organization VDC that is to be deleted (STRING)
        """
        try:
            if rollback:
                logger.info("RollBack: Deleting Target Org-Vdc")
            # splitting the org vdc id as per the requirement of the xml api
            orgVDCId = orgVDCId.split(':')[-1]
            # url to delete the org vdc
            url = "{}{}?force=true&recursive=true".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                                          vcdConstants.ORG_VDC_BY_ID.format(orgVDCId))
            # delete api to delete the org vdc
            response = self.restClientObj.delete(url, self.headers)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.accepted:
                task = responseDict["Task"]
                taskUrl = task["@href"]
                if taskUrl:
                    # checking the status of deleting org vdc task
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug('Organization VDC deleted successfully.')
                    return
            else:
                raise Exception('Failed to delete target Org VDC {}'.format(responseDict['Error']['@message']))
        except Exception:
            raise

    @isSessionExpired
    def deleteOrgVDCNetworks(self, orgVDCId, source=True, rollback=False):
        """
        Description :   Deletes all Organization VDC Networks from the specified OrgVDC
        Parameters  :   orgVDCId  -   Id of the Organization VDC (STRING)
                        source    -   Defaults to True meaning delete the NSX-V backed Org VDC Networks (BOOL)
                                      If set to False meaning delete the NSX-t backed Org VDC Networks (BOOL)
        """
        try:
            if rollback:
                logger.info("RollBack: Deleting Target Org VDC Networks")
            orgVDCNetworksErrorList = []

            dfwStatus = False
            if rollback:
                dfwStatus = True if self.rollback.apiData.get('OrgVDCGroupID') else False

            orgVDCNetworksList = self.getOrgVDCNetworks(orgVDCId, 'sourceOrgVDCNetworks', dfwStatus=dfwStatus, saveResponse=False)
            # iterating over the org vdc network list
            for orgVDCNetwork in orgVDCNetworksList:
                # url to delete the org vdc network
                url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                    vcdConstants.DELETE_ORG_VDC_NETWORK_BY_ID.format(orgVDCNetwork['id']))
                response = self.restClientObj.delete(url, self.headers)
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    if source:
                        # state check for NSX-V backed Org VDC
                        if orgVDCNetwork['networkType'] == 'ISOLATED':
                            # checking the status of deleting isolated network task
                            self._checkTaskStatus(taskUrl=taskUrl)
                        else:
                            # checking the status of deleting routed network task
                            self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug('Organization VDC Network deleted successfully.')
                    else:
                        # state check for NSX-t backed Org VDC
                        self._checkTaskStatus(taskUrl=taskUrl)
                else:
                    logger.debug('Failed to delete Organization VDC Network {}.{}'.format(orgVDCNetwork['name'],
                                                                                          response.json()['message']))
                    orgVDCNetworksErrorList.append(orgVDCNetwork['name'])
            if orgVDCNetworksErrorList:
                raise Exception(
                    'Failed to delete Org VDC networks {} - as it is in use'.format(orgVDCNetworksErrorList))
        except Exception:
            raise

    @isSessionExpired
    def deleteNsxVBackedOrgVDCEdgeGateways(self, orgVDCId):
        """
        Description :   Deletes all the Edge Gateways in the specified NSX-V Backed OrgVDC
        Parameters  :   orgVDCId  -   Id of the Organization VDC (STRING)
        """
        try:
            # retrieving the details of the org vdc edge gateway
            responseDict = self.getOrgVDCEdgeGateway(orgVDCId)
            if responseDict['values']:
                for orgVDCEdgeGateway in responseDict['values']:
                    orgVDCEdgeGatewayId = orgVDCEdgeGateway['id'].split(':')[-1]
                    # url to fetch edge gateway details
                    getUrl = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                           vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(orgVDCEdgeGatewayId))
                    getResponse = self.restClientObj.get(getUrl, headers=self.headers)
                    if getResponse.status_code == requests.codes.ok:
                        responseDict = xmltodict.parse(getResponse.content)
                        edgeGatewayDict = responseDict['EdgeGateway']
                        # checking if distributed routing is enabled on edge gateway, if so disabling it
                        if edgeGatewayDict['Configuration']['DistributedRoutingEnabled'] == 'true':
                            self.disableDistributedRoutingOnOrgVdcEdgeGateway(orgVDCEdgeGateway['id'])
                    # url to delete the edge gateway
                    deleteUrl = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                              vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(orgVDCEdgeGatewayId))
                    # delete api to delete edge gateway
                    delResponse = self.restClientObj.delete(deleteUrl, self.headers)
                    if delResponse.status_code == requests.codes.accepted:
                        taskUrl = delResponse.headers['Location']
                        # checking the status of deleting nsx-v backed edge gateway task
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug('Source Org VDC Edge Gateway deleted successfully.')
                    else:
                        delResponseDict = xmltodict.parse(delResponse.content)
                        raise Exception('Failed to delete Edge gateway {}:{}'.format(orgVDCEdgeGateway['name'],
                                                                                     delResponseDict['Error'][
                                                                                         '@message']))
            else:
                logger.warning('Edge Gateway doesnot exist')
        except Exception:
            raise

    @isSessionExpired
    def deleteNsxTBackedOrgVDCEdgeGateways(self, orgVDCId):
        """
        Description :   Deletes all the Edge Gateways in the specified NSX-t Backed OrgVDC
        Parameters  :   orgVDCId  -   Id of the Organization VDC (STRING)
        """
        try:
            logger.info("RollBack: Deleting Target Edge Gateway")
            # retrieving the details of the org vdc edge gateway
            responseDict = self.getOrgVDCEdgeGateway(orgVDCId)
            if responseDict['values']:
                for orgVDCEdgeGateway in responseDict['values']:
                    # url to fetch edge gateway details
                    url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                        vcdConstants.UPDATE_EDGE_GATEWAYS_BY_ID.format(orgVDCEdgeGateway['id']))
                    # delete api to delete the nsx-t backed edge gateway
                    response = self.restClientObj.delete(url, self.headers)
                    if response.status_code == requests.codes.accepted:
                        taskUrl = response.headers['Location']
                        # checking the status of deleting the nsx-t backed edge gateway
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug('Target Org VDC Edge Gateway deleted successfully.')
                    else:
                        raise Exception('Failed to delete Edge gateway {}:{}'.format(orgVDCEdgeGateway['name'],
                                                                                     response.json()['message']))
            else:
                logger.warning('Target Edge Gateway do not exist')
        except Exception:
            raise

    @description("disconnection of source routed Org VDC Networks from source Edge gateway")
    @remediate
    def disconnectSourceOrgVDCNetwork(self, orgVDCNetworkList, rollback=False):
        """
        Description : Disconnect source Org VDC network from edge gateway
        Parameters  : orgVdcNetworkList - Org VDC's network list for a specific Org VDC (LIST)
                      rollback - key that decides whether to perform rollback or not (BOOLEAN)
        """
        try:
            if not rollback:
                logger.info('Disconnecting source routed Org VDC Networks from source Edge gateway.')
            else:
                logger.info('Rollback: Reconnecting Source Org VDC Network to Edge Gateway')
            # list of networks disconnected successfully
            networkDisconnectedList = []
            orgVDCNetworksErrorList = []
            # iterating over the org vdc network list
            for orgVdcNetwork in orgVDCNetworkList:
                # checking only for nat routed Org VDC Network
                if orgVdcNetwork['networkType'] == "NAT_ROUTED":
                    # url to disconnect org vdc networks
                    url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                           vcdConstants.ALL_ORG_VDC_NETWORKS, orgVdcNetwork['id'])
                    response = self.restClientObj.get(url, self.headers)
                    responseDict = response.json()
                    # creating payload data as per the requirements
                    responseDict['connection'] = None
                    responseDict['networkType'] = 'ISOLATED'
                    del responseDict['status']
                    del responseDict['lastTaskFailureMessage']
                    # del(responseDict['guestVlanTaggingAllowed'])
                    del responseDict['retainNicResources']
                    del responseDict['crossVdcNetworkId']
                    del responseDict['crossVdcNetworkLocationId']
                    del responseDict['totalIpCount']
                    del responseDict['usedIpCount']
                    if rollback:
                        responseDict['connection'] = orgVdcNetwork['connection']
                        responseDict['networkType'] = 'NAT_ROUTED'
                    payloadDict = json.dumps(responseDict)
                    self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                    # put api to disconnect the org vdc networks
                    apiResponse = self.restClientObj.put(url, self.headers, data=payloadDict)
                    if apiResponse.status_code == requests.codes.accepted:
                        taskUrl = apiResponse.headers['Location']
                        # checking the status of the disconnecting org vdc network task
                        self._checkTaskStatus(taskUrl=taskUrl)
                        if not rollback:
                            logger.debug(
                                'Source Org VDC Network {} disconnected successfully.'.format(orgVdcNetwork['name']))
                            # saving network on successful disconnection to list
                            networkDisconnectedList.append(orgVdcNetwork)
                        else:
                            logger.debug(
                                'Source Org VDC Network {} reconnected successfully.'.format(orgVdcNetwork['name']))
                    else:
                        if rollback:
                            logger.debug('Rollback: Failed to reconnect Source Org VDC Network {}.'.format(
                                orgVdcNetwork['name']))
                        else:
                            logger.debug(
                                'Failed to disconnect Source Org VDC Network {}.'.format(orgVdcNetwork['name']))
                        orgVDCNetworksErrorList.append(orgVdcNetwork['name'])
                if orgVDCNetworksErrorList:
                    raise Exception('Failed to disconnect Org VDC Networks {}'.format(orgVDCNetworksErrorList))
        except Exception as exception:
            # reconnecting the networks in case of disconnection failure
            if networkDisconnectedList:
                self.disconnectSourceOrgVDCNetwork(networkDisconnectedList, rollback=True)
            raise exception

    @description("disconnection of source Edge gateway from external network")
    @remediate
    def reconnectOrDisconnectSourceEdgeGateway(self, sourceEdgeGatewayIdList, connect=True):
        """
        Description :  Disconnect source Edge Gateways from the specified OrgVDC
        Parameters  :   sourceEdgeGatewayId -   Id of the Organization VDC Edge gateway (STRING)
                        connect             -   Defaults to True meaning reconnects the source edge gateway (BOOL)
                                            -   if set False meaning disconnects the source edge gateway (BOOL)
        """
        try:
            if not connect:
                logger.info('Disconnecting source Edge gateway from external network.')
            else:
                logger.info('Rollback: Reconnecting source Edge gateway to external network.')

            for sourceEdgeGatewayId in sourceEdgeGatewayIdList:
                # Fetching edge gateway details from metadata corresponding to edge gateway id
                edgeGatewaydata = \
                    list(filter(lambda edgeGatewayData: edgeGatewayData['id'] == sourceEdgeGatewayId,
                                copy.deepcopy(self.rollback.apiData['sourceEdgeGateway'])))[0]
                orgVDCEdgeGatewayId = sourceEdgeGatewayId.split(':')[-1]
                # url to disconnect/reconnect the source edge gateway
                url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                    vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(orgVDCEdgeGatewayId))
                acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
                headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
                # retrieving the details of the edge gateway
                response = self.restClientObj.get(url, headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    if not responseDict['configuration']['gatewayInterfaces']['gatewayInterface'][0][
                        'connected'] and not connect:
                        logger.warning(
                            'Source Edge Gateway external network uplink - {} is already in disconnected state.'.format(
                                responseDict['name']))
                        continue
                    # establishing/disconnecting the edge gateway as per the connect flag
                    if not connect:
                        responseDict['configuration']['gatewayInterfaces']['gatewayInterface'][0]['connected'] = False
                    elif edgeGatewaydata['edgeGatewayUplinks'][0]['connected'] == True:
                        responseDict['configuration']['gatewayInterfaces']['gatewayInterface'][0]['connected'] = True

                        for index, uplink in enumerate(responseDict['configuration']['gatewayInterfaces']['gatewayInterface']):
                            if uplink['interfaceType'] == 'internal':
                                responseDict['configuration']['gatewayInterfaces']['gatewayInterface'].pop(index)
                                #responseDict['configuration']['gatewayInterfaces']['gatewayInterface'].pop()
                    else:
                        continue
                    payloadData = json.dumps(responseDict)
                    acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
                    self.headers["Content-Type"] = vcdConstants.XML_UPDATE_EDGE_GATEWAY
                    headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader,
                               'Content-Type': vcdConstants.JSON_UPDATE_EDGE_GATEWAY}
                    # updating the details of the edge gateway
                    response = self.restClientObj.put(url + '/action/updateProperties', headers, data=payloadData)
                    responseData = response.json()
                    if response.status_code == requests.codes.accepted:
                        taskUrl = responseData["href"]
                        if taskUrl:
                            # checking the status of connecting/disconnecting the edge gateway
                            self._checkTaskStatus(taskUrl=taskUrl)
                            logger.debug('Source Edge Gateway updated successfully.')
                            continue
                    else:
                        raise Exception('Failed to update source Edge Gateway {}'.format(responseData['message']))
        except:
            raise

    @description("Reconnection of target Edge gateway to T0 router")
    @remediate
    def reconnectTargetEdgeGateway(self):
        """
        Description : Reconnect Target Edge Gateway to T0 router
        """
        try:
            logger.info('Reconnecting target Edge gateway to T0 router.')
            data = self.rollback.apiData
            for targetEdgeGateway in data['targetEdgeGateway']:
                payloadDict = targetEdgeGateway
                del payloadDict['status']
                if self.rollback.apiData.get('OrgVDCGroupID'):
                    ownerRef = self.rollback.apiData['OrgVDCGroupID'].get(targetEdgeGateway['id'])
                    payloadDict['ownerRef'] = {'id': ownerRef}
                payloadDict['edgeGatewayUplinks'][0]['connected'] = True
                # edge gateway update URL
                url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EDGE_GATEWAYS,
                                       targetEdgeGateway['id'])
                # creating the payload data
                payloadData = json.dumps(payloadDict)
                self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                # put api to reconnect the target edge gateway
                response = self.restClientObj.put(url, self.headers, data=payloadData)
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    # checking the status of the reconnecting target edge gateway task
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug(
                        'Target Org VDC Edge Gateway {} reconnected successfully.'.format(targetEdgeGateway['name']))
                    continue
                else:
                    raise Exception(
                        'Failed to reconnect target Org VDC Edge Gateway {} {}'.format(targetEdgeGateway['name'],
                                                                                       response.json()['message']))
            logger.info('Successfully reconnected target Edge gateway to T0 router.')
        except:
            raise

    @description("getting the portgroup of source org vdc networks")
    @remediate
    def getPortgroupInfo(self, orgVdcNetworkList):
        """
        Description : Get Portgroup Info
        Parameters  : orgVdcNetworkList - List of source org vdc networks (LIST)
        """
        try:
            logger.info('Getting the portgroup of source org vdc networks.')
            data = self.rollback.apiData
            # url to get the port group details
            url = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                vcdConstants.GET_PORTGROUP_INFO)
            acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
            # retrieving the details of the port group
            response = self.restClientObj.get(url, headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                resultTotal = responseDict['total']
            pageNo = 1
            pageSizeCount = 0
            resultList = []
            logger.debug('Getting portgroup details')
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}&page={}&pageSize={}&format=records".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                                                       vcdConstants.GET_PORTGROUP_INFO, pageNo,
                                                                       vcdConstants.PORT_GROUP_PAGE_SIZE)
                response = self.restClientObj.get(url, headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    resultList.extend(responseDict['record'])
                    pageSizeCount += len(responseDict['record'])
                    logger.debug('Portgroup details result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
            logger.debug('Total Portgroup details result count = {}'.format(len(resultList)))
            logger.debug('Portgroup details successfully retrieved')
            networkList = [response for response in resultList if
                           response['networkName'] != '--']
            updatedNetworkList = [response for response in networkList if response['scopeType'] not in ['-1', '1']]
            portGroupList = [elem for elem in updatedNetworkList if
                             elem['networkName'] in list(value['name'] for value in orgVdcNetworkList)]
            data['portGroupList'] = portGroupList
            logger.info('Retrieved the portgroup of source org vdc networks.')
            return
        except:
            raise

    @isSessionExpired
    def createMoveVappVmPayload(self, vApp, targetOrgVDCId, rollback=False):
        """
        Description : Create vApp vm payload for move vApp api
        Parameters : vApp - dict containing source vApp details
                     targetOrgVDCId - target Org VDC Id (STRING)
                     rollback - whether to rollback vapp from T2V (BOOLEAN)
        """
        try:
            xmlPayloadData = ''
            data = self.rollback.apiData
            if rollback:
                targetStorageProfileList = [
                    data["sourceOrgVDC"]['VdcStorageProfiles']['VdcStorageProfile']] if isinstance(
                    data["sourceOrgVDC"]['VdcStorageProfiles']['VdcStorageProfile'], dict) else \
                data["sourceOrgVDC"]['VdcStorageProfiles']['VdcStorageProfile']
            else:
                targetStorageProfileList = [
                    data["targetOrgVDC"]['VdcStorageProfiles']['VdcStorageProfile']] if isinstance(
                    data["targetOrgVDC"]['VdcStorageProfiles']['VdcStorageProfile'], dict) else \
                data["targetOrgVDC"]['VdcStorageProfiles']['VdcStorageProfile']
            vmInVappList = []
            # get api call to retrieve the info of source vapp
            response = self.restClientObj.get(vApp['@href'], self.headers)
            responseDict = xmltodict.parse(response.content)
            if not responseDict['VApp'].get('Children'):
                return
            targetSizingPolicyOrgVDCUrn = 'urn:vcloud:vdc:{}'.format(targetOrgVDCId)
            # checking whether the vapp children vm are single/multiple
            if isinstance(responseDict['VApp']['Children']['Vm'], list):
                vmList = responseDict['VApp']['Children']['Vm']
            else:
                vmList = [responseDict['VApp']['Children']['Vm']]
            # iterating over the vms in vapp
            for vm in vmList:
                # retrieving the compute policy of vm
                computePolicyName = vm['ComputePolicy']['VmPlacementPolicy']['@name'] if vm['ComputePolicy'].get(
                    'VmPlacementPolicy') else None
                # retrieving the sizing policy of vm
                if vm['ComputePolicy'].get('VmSizingPolicy'):
                    if vm['ComputePolicy']['VmSizingPolicy']['@name'] != 'System Default':
                        sizingPolicyHref = vm['ComputePolicy']['VmSizingPolicy']['@href']
                    else:
                        # get the target System Default policy id
                        defaultSizingPolicy = self.getVmSizingPoliciesOfOrgVDC(targetSizingPolicyOrgVDCUrn,
                                                                               isTarget=True)
                        if defaultSizingPolicy:
                            defaultSizingPolicyId = defaultSizingPolicy[0]['id']
                            sizingPolicyHref = "{}{}/{}".format(
                                vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.VDC_COMPUTE_POLICIES, defaultSizingPolicyId)
                        else:
                            sizingPolicyHref = None
                else:
                    sizingPolicyHref = None
                storageProfileList = [storageProfile for storageProfile in targetStorageProfileList if
                                      storageProfile['@name'] == vm['StorageProfile']['@name']]
                if storageProfileList:
                    storageProfileHref = storageProfileList[0]['@href']
                else:
                    storageProfileHref = ''
                # gathering the vm's data required to create payload data and appending the dict to the 'vmInVappList' list
                vmInVappList.append(
                    {'name': vm['@name'], 'description': vm['Description'] if vm.get('Description') else '',
                     'href': vm['@href'], 'networkConnectionSection': vm['NetworkConnectionSection'],
                     'storageProfileHref': storageProfileHref, 'state': responseDict['VApp']['@status'],
                     'computePolicyName': computePolicyName, 'sizingPolicyHref': sizingPolicyHref,
                     'primaryNetworkConnectionIndex': vm['NetworkConnectionSection']['PrimaryNetworkConnectionIndex']})
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
            # iterating over the above saved vms list of source vapp
            for vm in vmInVappList:
                logger.debug('Getting VM - {} details'.format(vm['name']))
                # check whether the vapp state is powered on i.e 4 then poweron else poweroff
                if vm['state'] != "4":
                    state = "false"
                else:
                    state = "true"
                # checking multiple network connection details inside vm are single/multiple
                if isinstance(vm['networkConnectionSection']['NetworkConnection'], list):
                    networkConnectionList = vm['networkConnectionSection']['NetworkConnection']
                else:
                    networkConnectionList = [vm['networkConnectionSection']['NetworkConnection']]
                networkConnectionPayloadData = ''
                # creating payload for mutiple/single network connections in a vm
                for networkConnection in networkConnectionList:
                    if networkConnection['@network'] == 'none':
                        networkName = 'none'
                    else:
                        if rollback:
                            # remove the appended -v2t from network name
                            networkName = networkConnection['@network'].replace('-v2t', '')
                        else:
                            networkName = networkConnection['@network'] + '-v2t'
                    # checking for the 'IpAddress' attribute if present
                    if networkConnection.get('IpAddress'):
                        ipAddress = networkConnection['IpAddress']
                    else:
                        ipAddress = ""
                    payloadDict = {'networkName': networkName, 'ipAddress': ipAddress,
                                   'connected': networkConnection['IsConnected'],
                                   'macAddress': networkConnection['MACAddress'],
                                   'allocationModel': networkConnection['IpAddressAllocationMode'],
                                   'adapterType': networkConnection['NetworkAdapterType'],
                                   'networkConnectionIndex': networkConnection['NetworkConnectionIndex']
                                   }
                    payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                              componentName=vcdConstants.COMPONENT_NAME,
                                                              templateName=vcdConstants.VAPP_VM_NETWORK_CONNECTION_SECTION_TEMPLATE)
                    networkConnectionPayloadData += payloadData.strip("\"")
                # handling the case:- if both compute policy & sizing policy are absent
                if not vm["computePolicyName"] and not vm['sizingPolicyHref']:
                    payloadDict = {'vmHref': vm['href'], 'vmDescription': vm['description'], 'state': state,
                                   'storageProfileHref': vm['storageProfileHref'],
                                   'vmNetworkConnectionDetails': networkConnectionPayloadData,
                                   'primaryNetworkConnectionIndex': vm['primaryNetworkConnectionIndex']}
                    payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                              componentName=vcdConstants.COMPONENT_NAME,
                                                              templateName=vcdConstants.MOVE_VAPP_VM_TEMPLATE)
                # handling the case:- if either policy is present
                else:
                    # handling the case:- if compute policy is present and sizing policy is absent
                    if vm["computePolicyName"] and not vm['sizingPolicyHref']:
                        # retrieving the org vdc compute policy
                        allOrgVDCComputePolicesList = self.getOrgVDCComputePolicies()
                        # getting the list instance of compute policies of org vdc
                        orgVDCComputePolicesList = [allOrgVDCComputePolicesList] if isinstance(
                            allOrgVDCComputePolicesList, dict) else allOrgVDCComputePolicesList
                        if rollback:
                            targetProviderVDCid = data['sourceProviderVDC']['@id']
                        else:
                            targetProviderVDCid = data['targetProviderVDC']['@id']
                        # iterating over the org vdc compute policies
                        for eachComputPolicy in orgVDCComputePolicesList:
                            # checking if the org vdc compute policy name is same as the source vm's applied compute policy & org vdc compute policy id is same as that of target provider vdc's id
                            if eachComputPolicy["name"] == vm["computePolicyName"] and \
                                    eachComputPolicy["pvdcId"] == targetProviderVDCid:
                                # creating the href of compute policy that should be passed in the payload data for recomposing the vapp
                                href = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.VDC_COMPUTE_POLICIES,
                                                        eachComputPolicy["id"])
                        # if vm's compute policy does not match with org vdc compute policy or org vdc compute policy's id does not match with target provider vdc's id then href will be set none
                        # resulting into raising the exception that source vm's applied placement policy is absent in target org vdc
                        if not href:
                            raise Exception(
                                'Could not find placement policy {} in target Org VDC.'.format(vm["computePolicyName"]))
                        # creating the payload dictionary
                        payloadDict = {'vmHref': vm['href'], 'vmDescription': vm['description'], 'state': state,
                                       'storageProfileHref': vm['storageProfileHref'],
                                       'vmPlacementPolicyHref': href,
                                       'vmNetworkConnectionDetails': networkConnectionPayloadData,
                                       'primaryNetworkConnectionIndex': vm['primaryNetworkConnectionIndex']}
                        # creating the payload data
                        payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.MOVE_VAPP_VM_PLACEMENT_POLICY_TEMPLATE)
                    # handling the case:- if sizing policy is present and compute policy is absent
                    elif vm['sizingPolicyHref'] and not vm["computePolicyName"]:
                        # creating the payload dictionary
                        payloadDict = {'vmHref': vm['href'], 'vmDescription': vm['description'], 'state': state,
                                       'storageProfileHref': vm['storageProfileHref'],
                                       'sizingPolicyHref': vm['sizingPolicyHref'],
                                       'vmNetworkConnectionDetails': networkConnectionPayloadData,
                                       'primaryNetworkConnectionIndex': vm['primaryNetworkConnectionIndex']}
                        # creating the pauload data
                        payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.MOVE_VAPP_VM_SIZING_POLICY_TEMPLATE)
                    # handling the case:- if both policies are present
                    elif vm['sizingPolicyHref'] and vm["computePolicyName"]:
                        # retrieving the org vdc compute policy
                        allOrgVDCComputePolicesList = self.getOrgVDCComputePolicies()
                        # getting the list instance of compute policies of org vdc
                        orgVDCComputePolicesList = [allOrgVDCComputePolicesList] if isinstance(
                            allOrgVDCComputePolicesList, dict) else allOrgVDCComputePolicesList
                        if rollback:
                            targetProviderVDCid = data['sourceProviderVDC']['@id']
                        else:
                            targetProviderVDCid = data['targetProviderVDC']['@id']
                        # iterating over the org vdc compute policies
                        for eachComputPolicy in orgVDCComputePolicesList:
                            # checking if the org vdc compute policy name is same as the source vm's applied compute policy & org vdc compute policy id is same as that of target provider vdc's id
                            if eachComputPolicy["name"] == vm["computePolicyName"] and \
                                    eachComputPolicy["pvdcId"] == targetProviderVDCid:
                                # creating the href of compute policy that should be passed in the payload data for recomposing the vapp
                                href = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.VDC_COMPUTE_POLICIES,
                                                        eachComputPolicy["id"])
                        # if vm's compute policy does not match with org vdc compute policy or org vdc compute policy's id does not match with target provider vdc's id then href will be set none
                        # resulting into raising the exception that source vm's applied placement policy is absent in target org vdc
                        if not href:
                            raise Exception(
                                'Could not find placement policy {} in target Org VDC.'.format(vm["computePolicyName"]))
                        # creating the payload dictionary
                        payloadDict = {'vmHref': vm['href'], 'vmDescription': vm['description'], 'state': state,
                                       'storageProfileHref': vm['storageProfileHref'],
                                       'vmPlacementPolicyHref': href, 'sizingPolicyHref': vm['sizingPolicyHref'],
                                       'vmNetworkConnectionDetails': networkConnectionPayloadData,
                                       'primaryNetworkConnectionIndex': vm['primaryNetworkConnectionIndex']}
                        # creating the pauload data
                        payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.MOVE_VAPP_VM_COMPUTE_POLICY_TEMPLATE)
                xmlPayloadData += payloadData.strip("\"")
            return xmlPayloadData
        except Exception:
            raise

    @isSessionExpired
    def getOrgVDCStorageProfileDetails(self, orgVDCStorageProfileId):
        """
        Description :   Gets the details of the specified Org VDC Storage Profile ID
        Parameters  :   orgVDCStorageProfileId -   ID of the Org VDC Storage Profile (STRING)
        Returns     :   Details of the Org VDC Storage Profile (DICTIONARY)
        """
        try:
            logger.debug("Getting Org VDC Storage Profile details of {}".format(orgVDCStorageProfileId))
            # splitting the orgVDCStorageProfileId as per the requirement of the xml api call
            orgVDCStorageProfileId = orgVDCStorageProfileId.split(':')[-1]
            # url to get the vdc storage profile of specified id
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.VCD_STORAGE_PROFILE_BY_ID.format(orgVDCStorageProfileId))
            response = self.restClientObj.get(url, self.headers)
            responseDict = xmltodict.parse(response.content)
            return responseDict
        except Exception:
            raise

    @description("Checking ACL on target Org vdc")
    @remediate
    def createACL(self):
        """
        Description : Create ACL on Org VDC
        """
        try:
            logger.info('Checking ACL on target Org vdc')

            data = self.rollback.apiData
            # retrieving the source org vdc id & target org vdc is
            sourceOrgVDCId = data["sourceOrgVDC"]['@id'].split(':')[-1]
            targetOrgVDCId = data["targetOrgVDC"]['@id'].split(':')[-1]
            # url to get the access control in org vdc
            url = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                vcdConstants.GET_ACCESS_CONTROL_IN_ORG_VDC.format(sourceOrgVDCId))
            acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
            # get api call to retrieve the access control details in source org vdc
            response = self.restClientObj.get(url, headers)
            data = json.loads(response.content)
            if not data['accessSettings']:
                logger.debug('ACL doesnot exist on source Org VDC')
                return
            # url to create access control in target org vdc
            url = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                vcdConstants.CREATE_ACCESS_CONTROL_IN_ORG_VDC.format(targetOrgVDCId))
            acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader,
                       'Content-Type': vcdConstants.CONTROL_ACCESS_CONTENT_TYPE}
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.json')
            # creating the payload dictionary
            payloadDict = {'isShared': data['isSharedToEveryone'],
                           'everyoneAccess': data['everyoneAccessLevel'] if data['everyoneAccessLevel'] else "Null"}
            # creating the payload data
            payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='json',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.CREATE_ORG_VDC_ACCESS_CONTROL_TEMPLATE)
            accessSettingsList = []
            # iterating over the access settings of source org vdc
            for subjectData in data['accessSettings']['accessSetting']:
                userData = {"subject": {"href": subjectData['subject']['href']},
                            "accessLevel": subjectData['accessLevel']}
                accessSettingsList.append(userData)
            jsonData = json.loads(payloadData)
            # attaching the access settings to the payload data
            jsonData['accessSettings'] = {'accessSetting': accessSettingsList}
            payloadData = json.dumps(jsonData)
            # put api to create access control in target org vdc
            response = self.restClientObj.put(url, headers, data=payloadData)
            if response.status_code != requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
                raise Exception(
                    'Failed to create target ACL on target Org VDC {}'.format(responseDict['Error']['@message']))
            logger.info('Successfully created ACL on target Org vdc')
        except Exception:
            raise

    @description("application of vm placement policy on target Org vdc")
    @remediate
    def applyVDCPlacementPolicy(self):
        """
        Description : Applying VM placement policy on vdc
        """
        try:
            data = self.rollback.apiData
            computePolicyHrefList = []
            # retrieving the target org vdc id, target provider vdc id & compute policy list of source from apiOutput.json
            targetOrgVDCId = data['targetOrgVDC']['@id'].split(':')[-1]
            targetProviderVDCId = data['targetProviderVDC']['@id']
            if not data.get('sourceOrgVDCComputePolicyList'):
                logger.debug('No source Org VDC compute Policy exist')
                return
            logger.info('Applying vm placement policy on target Org vdc')
            sourcePolicyList = data['sourceOrgVDCComputePolicyList']
            # getting list instance of sourcePolicyList
            sourceComputePolicyList = [sourcePolicyList] if isinstance(sourcePolicyList, dict) else sourcePolicyList
            allOrgVDCComputePolicesList = self.getOrgVDCComputePolicies()
            # getting list instance of org vdc compute policies
            orgVDCComputePolicesList = [allOrgVDCComputePolicesList] if isinstance(allOrgVDCComputePolicesList,
                                                                                   dict) else allOrgVDCComputePolicesList
            # iterating over the org vdc compute policies
            for eachComputePolicy in orgVDCComputePolicesList:
                if eachComputePolicy["pvdcId"] == targetProviderVDCId:
                    # if compute policy's id is same as target provider vdc id and compute policy is not the system default
                    if eachComputePolicy["name"] != 'System Default':
                        # iterating over the source compute policies
                        for computePolicy in sourceComputePolicyList:
                            if computePolicy['@name'] == eachComputePolicy['name'] and eachComputePolicy['name'] != \
                                    data['targetOrgVDC']['DefaultComputePolicy']['@name']:
                                # creating the href of the org vdc compute policy
                                href = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.VDC_COMPUTE_POLICIES,
                                                        eachComputePolicy["id"])
                                computePolicyHrefList.append({'href': href})
            # url to get the compute policy details of target org vdc
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_COMPUTE_POLICY.format(targetOrgVDCId))
            acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
            # get api call to retrieve the target org vdc compute policy details
            response = self.restClientObj.get(url, headers)
            data = json.loads(response.content)
            alreadyPresentComputePoliciesList = []
            payloadDict = {}
            for computePolicy in data['vdcComputePolicyReference']:
                if computePolicy['href'] not in computePolicyHrefList:
                    # getting the list of compute policies which are already
                    alreadyPresentComputePoliciesList.append(
                        {'href': computePolicy['href'], 'id': computePolicy['id'], 'name': computePolicy['name']})
            payloadDict['vdcComputePolicyReference'] = alreadyPresentComputePoliciesList + computePolicyHrefList
            acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader,
                       'Content-Type': vcdConstants.GENERAL_JSON_CONTENT_TYPE}
            # creating the payload data
            payloadData = json.dumps(payloadDict)
            response = self.restClientObj.put(url, headers, data=payloadData)
            if response.status_code == requests.codes.ok:
                # there exists atleast single placement policy in source org vdc, so checking the computPolicyHrefList
                if computePolicyHrefList:
                    logger.debug('Successfully applied vm placement policy on target VDC')
            else:
                raise Exception(
                    'Failed to apply vm placement policy on target VDC {}'.format(response.json()['message']))
        except Exception:
            # setting the delete target org vdc flag
            self.DELETE_TARGET_ORG_VDC = True
            raise

    @description("Enabling Affinity Rules in Target VDC")
    @remediate
    def enableTargetAffinityRules(self):
        """
        Description :   Enable Affinity Rules in Target VDC
        """
        try:
            data = self.rollback.apiData
            # reading the data from the apiOutput.json
            targetOrgVdcId = data['targetOrgVDC']['@id']
            targetvdcid = targetOrgVdcId.split(':')[-1]
            # checking if affinity rules present in source
            if data.get('sourceVMAffinityRules'):
                logger.info('Configuring target Org VDC affinity rules')
                sourceAffinityRules = data['sourceVMAffinityRules'] if isinstance(data['sourceVMAffinityRules'],
                                                                                  list) else [
                    data['sourceVMAffinityRules']]
                # iterating over the affinity rules
                for sourceAffinityRule in sourceAffinityRules:
                    affinityID = sourceAffinityRule['@id']
                    # url to enable/disable the affinity rules
                    # url = vcdConstants.ENABLE_DISABLE_AFFINITY_RULES.format(self.ipAddress, affinityID)
                    url = "{}{}".format(vcdConstants.AFFINITY_URL.format(self.ipAddress, targetvdcid), affinityID)
                    filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
                    vmReferencesPayloadData = ''
                    for eachVmReference in sourceAffinityRule['VmReferences']['VmReference']:
                        payloadDict = {'vmHref': eachVmReference['@href'],
                                       'vmId': eachVmReference['@id'],
                                       'vmName': eachVmReference['@name'],
                                       'vmType': eachVmReference['@type']}
                        payloadData = self.vcdUtils.createPayload(filePath,
                                                                  payloadDict,
                                                                  fileType='yaml',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.VM_REFERENCES_TEMPLATE_NAME)
                        vmReferencesPayloadData += payloadData.strip("\"")
                    isEnabled = "true" if sourceAffinityRule['IsEnabled'] == "true" else "false"
                    payloadDict = {'affinityRuleName': sourceAffinityRule['Name'],
                                   'isEnabled': isEnabled,
                                   'isMandatory': "true" if sourceAffinityRule['IsMandatory'] == "true" else "false",
                                   'polarity': sourceAffinityRule['Polarity'],
                                   'vmReferences': vmReferencesPayloadData}
                    payloadData = self.vcdUtils.createPayload(filePath,
                                                              payloadDict,
                                                              fileType='yaml',
                                                              componentName=vcdConstants.COMPONENT_NAME,
                                                              templateName=vcdConstants.ENABLE_DISABLE_AFFINITY_RULES_TEMPLATE_NAME)
                    payloadData = json.loads(payloadData)
                    self.headers['Content-Type'] = vcdConstants.GENERAL_XML_CONTENT_TYPE
                    # put api call to enable / disable affinity rules
                    response = self.restClientObj.put(url, self.headers, data=payloadData)
                    responseDict = xmltodict.parse(response.content)
                    if response.status_code == requests.codes.accepted:
                        task_url = response.headers['Location']
                        # checking the status of the enabling/disabling affinity rules task
                        self._checkTaskStatus(taskUrl=task_url)
                        logger.debug('Affinity Rules got updated successfully in Target')
                    else:
                        raise Exception(
                            'Failed to update Affinity Rules in Target {}'.format(responseDict['Error']['@message']))
                logger.info('Successfully configured target Org VDC affinity rules')
        except Exception:
            raise

    @isSessionExpired
    def renameOrgVDC(self, sourceOrgVDCName, targetVDCId):
        """
        Description :   Renames the target Org VDC
        Parameters  :   sourceOrgVDCName    - name of the source org vdc (STRING)
                        targetVDCId         - id of the target org vdc (STRING)
        """
        try:
            # splitting the target org vdc id as per the requirement of xml api
            targetVDCId = targetVDCId.split(':')[-1]
            acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
            # url to get the target org vdc details
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_BY_ID.format(targetVDCId))
            # get api call to retrieve the target org vdc details
            response = self.restClientObj.get(url, headers=headers)
            responseDict = response.json()
            # creating the payload data by just changing the name of org vdc same as source org vdc
            responseDict['name'] = sourceOrgVDCName
            payloadData = json.dumps(responseDict)
            headers['Content-Type'] = vcdConstants.VDC_RENAME_CONTENT_TYPE
            # put api call to update the target org vdc name
            response = self.restClientObj.put(url, headers=headers, data=payloadData)
            responseData = response.json()
            if response.status_code == requests.codes.accepted:
                taskUrl = responseData["href"]
                if taskUrl:
                    # checking the status of renaming target org vdc task
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug('Renamed Org VDC to {} successfully'.format(responseDict['name']))
                return response
            raise Exception("Failed to rename the Org VDC {}".format(responseData['message']))
        except Exception:
            raise

    @isSessionExpired
    def getVmSizingPoliciesOfOrgVDC(self, orgVdcId, isTarget=False):
        """
        Description :   Fetches the list of vm sizing policies assigned to the specified Org VDC
        Parameters  :   orgVdcId    -   ID of the org VDC (STRING)
                        isTarget - True if its target Org VDC else False
        """
        try:
            logger.debug("Getting the VM Sizing Policy of Org VDC {}".format(orgVdcId))
            # url to retrieve the vm sizing policy details of the vm
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_VM_SIZING_POLICY.format(orgVdcId))
            # get api call to retrieve the vm sizing policy of the vm
            response = self.restClientObj.get(url, headers=self.headers)
            responseDict = response.json()
            if response.status_code == requests.codes.ok:
                logger.debug("Retrieved the VM Sizing Policy of Org VDC {} successfully".format(orgVdcId))
                if not isTarget:
                    # getting the source vm sizing policy excluding the policy named 'System Default'
                    sourceOrgVDCSizingPolicyList = [response for response in responseDict['values'] if
                                                    response['name'] != 'System Default']
                else:
                    # getting the source vm sizing policy for the policy named 'System Default'
                    sourceOrgVDCSizingPolicyList = [response for response in responseDict['values'] if
                                                    response['name'] == 'System Default']
                return sourceOrgVDCSizingPolicyList
            raise Exception("Failed to retrieve VM Sizing Policies of Organization VDC {} {}".format(orgVdcId,
                                                                                                     responseDict[
                                                                                                         'message']))
        except Exception:
            raise

    @description("application of vm sizing policy on target Org vdc")
    @remediate
    def applyVDCSizingPolicy(self):
        """
        Description :   Assigns the VM Sizing Policy to the specified OrgVDC
        """
        try:
            logger.info('Applying vm sizing policy on target Org vdc')

            data = self.rollback.apiData
            # retrieving the target org vdc name & id
            targetOrgVdcName = data['targetOrgVDC']['@name']
            targetOrgVdcId = data['targetOrgVDC']['@id']
            # retrieving the source org vdc id
            sourceOrgVdcId = data['sourceOrgVDC']['@id']
            # retrieving the source org vdc vm sizing policy
            sourceSizingPoliciesList = self.getVmSizingPoliciesOfOrgVDC(sourceOrgVdcId)
            if isinstance(sourceSizingPoliciesList, dict):
                sourceSizingPoliciesList = [sourceSizingPoliciesList]
            # iterating over the source org vdc vm sizing policies
            for eachPolicy in sourceSizingPoliciesList:
                # url to assign sizing policies to the target org vdc
                url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                    vcdConstants.ASSIGN_COMPUTE_POLICY_TO_VDC.format(eachPolicy['id']))
                payloadDict = [{"name": targetOrgVdcName,
                                "id": targetOrgVdcId}]
                # creating the payload data
                payloadData = json.dumps(payloadDict)
                self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                # post api call to assign the sizing policies to the target org vdc
                response = self.restClientObj.post(url, headers=self.headers, data=payloadData)
                if response.status_code == requests.codes.ok:
                    logger.debug("VM Sizing Policy {} assigned to Org VDC {} successfully".format(eachPolicy['name'],
                                                                                                  targetOrgVdcName))
                else:
                    raise Exception("Failed to assign VM Sizing Policy {} to Org VDC {} {}".format(eachPolicy['name'],
                                                                                                   targetOrgVdcName,
                                                                                                   response.json()[
                                                                                                       'message']))
        except Exception:
            self.DELETE_TARGET_ORG_VDC = True
            raise

    @description("disconnection of target Org VDC Networks")
    @remediate
    def disconnectTargetOrgVDCNetwork(self):
        """
        Description : Disconnect target Org VDC networks
        """
        try:
            logger.info('Disconnecting target Org VDC Networks.')
            targetOrgVDCId = self.rollback.apiData['targetOrgVDC']['@id']
            targetOrgVDCNetworkList = self.getOrgVDCNetworks(targetOrgVDCId, 'targetOrgVDCNetworks', saveResponse=False)
            # retrieving the target org vdc network list
            for vdcNetwork in targetOrgVDCNetworkList:
                # handling only the routed networks
                if vdcNetwork['networkType'] == "NAT_ROUTED":
                    vdcNetworkID = vdcNetwork['id']
                    # url to disconnect the target org vdc network
                    url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                           vcdConstants.ALL_ORG_VDC_NETWORKS,
                                           vdcNetworkID)
                    # creating the payload data
                    vdcNetwork['connection'] = None
                    vdcNetwork['networkType'] = 'ISOLATED'
                    payloadData = json.dumps(vdcNetwork)
                    self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                    # put api call to disconnect the target org vdc network
                    response = self.restClientObj.put(url, self.headers, data=payloadData)
                    if response.status_code == requests.codes.accepted:
                        taskUrl = response.headers['Location']
                        # checking the status of disconnecting the target org vdc network task
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug(
                            'Disconnected target Org VDC network - {} successfully.'.format(vdcNetwork['name']))
                    else:
                        response = response.json()
                        raise Exception('Failed to disconnect target Org VDC network {} - {}'.format(vdcNetwork['name'],
                                                                                                     response[
                                                                                    'message']))
            logger.info('Successfully disconnected target Org VDC Networks.')
        except Exception:
            raise

    @description("Reconnection of target Org VDC Networks")
    @remediate
    def reconnectOrgVDCNetworks(self, sourceOrgVDCId, targetOrgVDCId, source=True):
        """
        Description :   Reconnects the Org VDC networks of source/ target Org VDC
        Parameters  :   source  -   Defaults to True meaning reconnect the Source Org VDC Networks (BOOL)
                                -   if False meaning reconnect the Target Org VDC Networks (BOOL)
        """
        try:
            logger.info('Reconnecting target Org VDC Networks.')
            data = self.rollback.apiData
            # checking whether to reconnect the org vdc  networks of source or target, and getting the org vdc networks as per the source flag
            if source:
                OrgVDCNetworkList = self.retrieveNetworkListFromMetadata(sourceOrgVDCId, orgVDCType='source')
            else:
                OrgVDCNetworkList = self.retrieveNetworkListFromMetadata(targetOrgVDCId, orgVDCType='target')
            # iterating over the org vdc networks
            for vdcNetwork in OrgVDCNetworkList:
                # handling only routed networks
                if vdcNetwork['networkType'] == "NAT_ROUTED":
                    vdcNetworkID = vdcNetwork['id']
                    # url to reconnect the org vdc network
                    url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                           vcdConstants.ALL_ORG_VDC_NETWORKS,
                                           vdcNetworkID)
                    # creating payload using data from apiOutput.json
                    payloadData = json.dumps(vdcNetwork)
                    self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                    # put api call to reconnect the org vdc
                    response = self.restClientObj.put(url, self.headers, data=payloadData)
                    srcTgt = "source" if source else "target"
                    if response.status_code == requests.codes.accepted:
                        taskUrl = response.headers['Location']
                        # checking the status of recoonecting the specified org vdc
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug('Reconnected {} Org VDC network - {} successfully.'.format(srcTgt, vdcNetwork['name']))
                    else:
                        response = response.json()
                        raise Exception('Failed to reconnect {} Org VDC network {} - {}'.format(srcTgt, vdcNetwork['name'],
                                                                                                response['message']))
        except Exception:
            raise

    @isSessionExpired
    def disableDistributedRoutingOnOrgVdcEdgeGateway(self, orgVDCEdgeGatewayId):
        """
        Description :   Disables the Distributed Routing on the specified edge gateway
        Parameters  :   orgVDCEdgeGatewayId -   ID of the edge gateway (STRING)
        """
        try:
            # splitting the edge gateway id as per the requuirements of xml api
            edgeGatewayId = orgVDCEdgeGatewayId.split(':')[-1]
            # url to disable distributed routing on specified edge gateway
            url = "{}{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                  vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(edgeGatewayId),
                                  vcdConstants.DISABLE_EDGE_GATEWAY_DISTRIBUTED_ROUTING)
            # post api call to disable distributed routing on the specified edge gateway
            response = self.restClientObj.post(url, self.headers)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.accepted:
                task = responseDict["Task"]
                taskUrl = task["@href"]
                if taskUrl:
                    # checking the status of disabling the edge gateway
                    self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug("Disabled Distributed Routing on source edge gateway successfully")
            else:
                raise Exception("Failed to disable Distributed Routing on source edge gateway {}".format(responseDict['Error']['@message']))
        except Exception:
            raise

    @description("Update DHCP on Target Org VDC Networks")
    @remediate
    def _updateDhcpInOrgVdcNetworks(self, url, payload):
        """
            Description : Put API request to configure DHCP
            Parameters  : url - URL path (STRING)
                          payload - source dhcp configuration to be updated (DICT)
        """
        try:
            logger.debug('Updating DHCP configuration in OrgVDC network')
            self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
            response = self.restClientObj.put(url, self.headers, data=json.dumps(payload))
            if response.status_code == requests.codes.accepted:
                taskUrl = response.headers['Location']
                # checking the status of configuring the dhcp on target org vdc networks task
                self._checkTaskStatus(taskUrl=taskUrl)
                # setting the configStatus flag meaning the particular DHCP rule is configured successfully in order to skip its reconfiguration
                logger.debug('DHCP pool created successfully.')
            else:
                errorResponse = response.json()
                raise Exception('Failed to create DHCP  - {}'.format(errorResponse['message']))
        except Exception:
            raise


    @isSessionExpired
    def configureNetworkProfile(self, targetOrgVDCId):
        """
            Description : Configure network profile on target OrgVDC
            Parameters  : targetOrgVDCId    -   Id of the target organization VDC in URN format (STRING)
        """
        try:
            logger.debug('Configuring network profile on target orgVDC')
            data = self.rollback.apiData
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.NETWORK_PROFILE.format(targetOrgVDCId))
            # payload to configure edge cluster details from target edge gateway
            payload = {
                "servicesEdgeCluster": {
                    "edgeClusterRef": {
                        "name": data['targetEdgeGateway'][0]['edgeClusterConfig']['primaryEdgeCluster']['edgeClusterRef']['name'],
                        "id": data['targetEdgeGateway'][0]['edgeClusterConfig']['primaryEdgeCluster']['edgeClusterRef']['id']
                    },
                    "backingId": data['targetEdgeGateway'][0]['edgeClusterConfig']['primaryEdgeCluster']['backingId']
                }
            }
            self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
            response = self.restClientObj.put(url, self.headers, data=json.dumps(payload))
            if response.status_code == requests.codes.accepted:
                taskUrl = response.headers['Location']
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug('Network profile on target OrgVDC is configured')
            else:
                raise Exception('Failed to configure network profile on target OrgVDC')
        except Exception:
            raise

    @description("Configuration of DHCP on Target Org VDC Networks")
    @remediate
    def configureDHCP(self, targetOrgVDCId):
        """
        Description : Configure DHCP on Target Org VDC networks
        Parameters  : targetOrgVDCId    -   Id of the target organization VDC (STRING)
        """
        try:
            logger.debug("Configuring DHCP on Target Org VDC Networks")
            data = self.rollback.apiData
            for sourceEdgeGatewayDHCP in data['sourceEdgeGatewayDHCP'].values():
                # checking if dhcp is enabled on source edge gateway
                if not sourceEdgeGatewayDHCP['enabled']:
                    logger.debug('DHCP service is not enabled or configured in Source Edge Gateway')
                else:
                    # retrieving the dhcp rules of the source edge gateway
                    dhcpRules = sourceEdgeGatewayDHCP['ipPools']['ipPools'] if isinstance(
                        sourceEdgeGatewayDHCP['ipPools']['ipPools'], list) else [
                        sourceEdgeGatewayDHCP['ipPools']['ipPools']]
                    payloaddict = {}
                    # iterating over the source edge gateway dhcp rules
                    for iprange in dhcpRules:
                        # if configStatus flag is already set means that the dhcp rule is already configured, if so then skipping the configuring of same rule and moving to the next dhcp rule
                        if iprange.get('configStatus'):
                            continue
                        start = iprange['ipRange'].split('-')[0]
                        end = iprange['ipRange'].split('-')[-1]
                        # iterating over the target org vdc networks
                        for vdcNetwork in self.retrieveNetworkListFromMetadata(targetOrgVDCId, orgVDCType='target'):
                            # handling only the routed networks
                            if vdcNetwork['networkType'] == "NAT_ROUTED":
                                # checking the first three octets of source ip range with first three octets of target networks,
                                # first three octets are same then configuring dhcp on target
                                dhcp_check_list = start.split('.')
                                dhcp_check_list.pop()
                                dhcp_check = '.'.join(dhcp_check_list)
                                for gateway in vdcNetwork['subnets']['values']:
                                    vdcNetwork_check_list = gateway['gateway'].split('.')
                                    vdcNetwork_check_list.pop()
                                    vdcNetwork_check = '.'.join(vdcNetwork_check_list)
                                    if dhcp_check == vdcNetwork_check:
                                        vdcNetworkID = vdcNetwork['id']
                                        # creating payload data
                                        payloaddict['enabled'] = "true" if sourceEdgeGatewayDHCP['enabled'] else "false"
                                        payloaddict['dhcpPools'] = [{
                                            "enabled": "true" if sourceEdgeGatewayDHCP['enabled'] else "false",
                                            "ipRange": {
                                                "startAddress": start,
                                                "endAddress": end
                                            },
                                            "defaultLeaseTime": 0
                                        }]
                                        for pool in payloaddict['dhcpPools']:
                                            if iprange['leaseTime'] == "infinite":
                                                pool['maxLeaseTime'] = 4294967
                                            else:
                                                pool['maxLeaseTime'] = iprange['leaseTime']
                                        # url to configure dhcp on target org vdc networks
                                        url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                               vcdConstants.ALL_ORG_VDC_NETWORKS,
                                                               vcdConstants.DHCP_ENABLED_FOR_ORG_VDC_NETWORK_BY_ID.format(
                                                                   vdcNetworkID))
                                        response = self.restClientObj.get(url, self.headers)
                                        if response.status_code == requests.codes.ok:
                                            responseDict = response.json()
                                            dhcpPools = responseDict['dhcpPools'] + payloaddict['dhcpPools'] if \
                                                responseDict['dhcpPools'] else payloaddict['dhcpPools']
                                            payloaddict['dhcpPools'] = dhcpPools
                                            # payloadData = json.dumps(payloaddict)
                                            # put api call to configure dhcp on target org vdc networks
                                            self._updateDhcpInOrgVdcNetworks(url, payloaddict)
                                            # setting the configStatus flag meaning the particular DHCP rule is configured successfully in order to skip its reconfiguration
                                            iprange['configStatus'] = True
                                        else:
                                            errorResponse = response.json()
                                            raise Exception(
                                                'Failed to fetch DHCP service - {}'.format(errorResponse['message']))
            if self.version == vcdConstants.API_VERSION_ZEUS and data.get('OrgVDCIsolatedNetworkDHCP', []) != []:
                data = self.rollback.apiData
                targetOrgVDCNetworksList = data['targetOrgVDCNetworks'].keys()
                self.configureNetworkProfile(targetOrgVDCId)
                self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                for eachDHCPConfig in data['OrgVDCIsolatedNetworkDHCP']:
                    payload = dict()
                    orgVDCNetworkName, OrgVDCIsolatedNetworkDHCPDetails = list(eachDHCPConfig.items())[0]
                    payload["enabled"] = OrgVDCIsolatedNetworkDHCPDetails['enabled']
                    payload["leaseTime"] = OrgVDCIsolatedNetworkDHCPDetails['leaseTime']
                    payload["dhcpPools"] = list()
                    firstPoolIndex = 0
                    if OrgVDCIsolatedNetworkDHCPDetails["dhcpPools"] != None:
                        for eachDhcpPool in OrgVDCIsolatedNetworkDHCPDetails["dhcpPools"]:
                            currentPoolDict = dict()
                            currentPoolDict["enabled"] = eachDhcpPool['enabled']
                            if firstPoolIndex == 0:
                                ipToBeRemoved = OrgVDCIsolatedNetworkDHCPDetails["dhcpPools"][0]['ipRange']['startAddress']
                                newStartIpAddress = ipToBeRemoved.split('.')
                                newStartIpAddress[-1] = str(int(newStartIpAddress[-1]) + 1)
                                currentPoolDict["ipRange"] = {"startAddress": '.'.join(newStartIpAddress),
                                                              "endAddress": eachDhcpPool['ipRange']['endAddress']}
                                firstPoolIndex += 1
                            else:
                                currentPoolDict["ipRange"] = {"startAddress": eachDhcpPool['ipRange']['startAddress'],
                                                              "endAddress": eachDhcpPool['ipRange']['endAddress']}
                            currentPoolDict["maxLeaseTime"] = eachDhcpPool['maxLeaseTime']
                            currentPoolDict["defaultLeaseTime"] = eachDhcpPool['defaultLeaseTime']
                            payload["dhcpPools"].append(currentPoolDict)
                        payload['mode'] = "NETWORK"
                        payload['ipAddress'] = ipToBeRemoved
                    else:
                        logger.debug('DHCP pools not present in OrgVDC Network: {}'.format(orgVDCNetworkName))
                        continue
                    if orgVDCNetworkName + '-v2t' in targetOrgVDCNetworksList:
                        url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                            vcdConstants.ORG_VDC_NETWORK_DHCP.format(
                                                data['targetOrgVDCNetworks'][orgVDCNetworkName + '-v2t']['id']))
                        self._updateDhcpInOrgVdcNetworks(url, payload)
            else:
                logger.debug('Isolated OrgVDC networks not present on source OrgVDC')
        except:
            raise

    def prepareTargetVDC(self, sourceOrgVDCId, vcdDict, nsxObj, sourceOrgVDCName):
        """
        Description :   Preparing Target VDC
        Parameters  :   metadata   -   metadata from source Org VDC (LIST)
                        nsxObj      -   NSXTOperations class object
        """
        try:
            orgVdcNetworkList = self.getOrgVDCNetworks(sourceOrgVDCId, 'sourceOrgVDCNetworks', saveResponse=False)

            # creating target Org VDC
            self.createOrgVDC()

            # applying the vm placement policy on target org vdc
            self.applyVDCPlacementPolicy()

            # applying the vm sizing policy on target org vdc
            self.applyVDCSizingPolicy()

            # checking the acl on target org vdc
            self.createACL()

            # creating target Org VDC Edge Gateway
            self.createEdgeGateway(vcdDict, nsxObj)

            # only if source org vdc networks exist
            if orgVdcNetworkList:
                # creating target Org VDC networks
                self.createOrgVDCNetwork(orgVdcNetworkList)

                # disconnecting target Org VDC networks
                self.disconnectTargetOrgVDCNetwork()
            else:
                logger.debug('Skipping Target Org VDC Network creation as no source Org VDC network exist.')
                # If not source Org VDC networks are not present target Org VDC networks will also be empty
                self.rollback.apiData['targetOrgVDCNetworks'] = {}

            if self.version == vcdConstants.API_VERSION_ZEUS:
                # creating orgVdcGroups
                self.createOrgvDCGroup(sourceOrgVDCName)

            # enable the promiscous mode and forged transmit of source org vdc networks
            self.enablePromiscModeForgedTransmit(orgVdcNetworkList)

            # get the portgroup of source org vdc networks
            self.getPortgroupInfo(orgVdcNetworkList)

            # Migrating metadata from source org vdc to target org vdc
            self.migrateMetadata()
        except Exception as err:
            raise

    def configureTargetVDC(self):
        """
        Description :   Configuring Target VDC
        Parameters  :   metadata - status of tasks performed under configuration of target vdc (DICT)
        """
        try:
            # Fetching data from metadata
            data = self.rollback.apiData
            sourceEdgeGatewayIdList = data['sourceEdgeGatewayId']
            sourceOrgVDCId = self.rollback.apiData['sourceOrgVDC']['@id']
            targetOrgVDCId = self.rollback.apiData['targetOrgVDC']['@id']
            orgVdcNetworkList = self.retrieveNetworkListFromMetadata(sourceOrgVDCId, orgVDCType='source')
            targetOrgVDCNetworkList = self.retrieveNetworkListFromMetadata(targetOrgVDCId, orgVDCType='target')

            # edgeGatewayId = copy.deepcopy(data['targetEdgeGateway']['id'])
            if orgVdcNetworkList:
                # disconnecting source org vdc networks from edge gateway
                self.disconnectSourceOrgVDCNetwork(orgVdcNetworkList)

            # connecting dummy uplink to edge gateway
            self.connectUplinkSourceEdgeGateway(sourceEdgeGatewayIdList)

            # disconnecting source org vdc edge gateway from external
            self.reconnectOrDisconnectSourceEdgeGateway(sourceEdgeGatewayIdList, connect=False)

            if targetOrgVDCNetworkList:
                # reconnecting target Org VDC networks
                self.reconnectOrgVDCNetworks(sourceOrgVDCId, targetOrgVDCId, source=False)

            # configuring firewall security groups
            self.configureFirewall(networktype=True)
            # Restoring rollback key
            self.rollback.key = 'reconnectOrgVDCNetworks'

            # configuring dhcp service target Org VDC networks
            self.configureDHCP(targetOrgVDCId)
            # Restoring rollback key
            self.rollback.key = 'reconnectOrgVDCNetworks'

            if self.version == vcdConstants.API_VERSION_ZEUS:
                # increase in scope of Target edgegateways
                self.increaseScopeOfEdgegateways()
                # # increase in scope of Target ORG VDC networks
                self.increaseScopeforNetworks()
                # Enable DFW in the orgVDC groups
                self.enableDFWinOrgvdcGroup()
                # Configure DFW in org VDC groups
                self.configureDFW(sourceOrgVDCId=sourceOrgVDCId)
                # Restoring rollback key
                self.rollback.key = 'reconnectOrgVDCNetworks'

            # reconnecting target org vdc edge gateway from T0
            self.reconnectTargetEdgeGateway()
            # Restoring rollback key
            self.rollback.key = 'reconnectTargetEdgeGateway'
        except Exception:
            raise
        finally:
            # saving rollback key in case of failure
            self.createMetaDataInOrgVDC(sourceOrgVDCId,
                                        metadataDict={'rollbackKey': self.rollback.key}, domain='system')

    def migrateCatalogItems(self, sourceOrgVDCId, targetOrgVDCId, orgUrl):
        """
        Description : Migrating Catalog Items - vApp Templates and Media & deleting catalog thereafter
        Parameters  :   sourceOrgVDCId  - source Org VDC id (STRING)
                        targetOrgVDCId  - target Org VDC id (STRING)
                        orgUrl          - Organization url (STRING)
        """
        try:
            sourceOrgVDCId = sourceOrgVDCId.split(':')[-1]
            # url to get the details of source org vdc
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_BY_ID.format(sourceOrgVDCId))
            # get api call to retrieve the source org vdc details
            sourceOrgVDCResponse = self.restClientObj.get(url, self.headers)
            sourceOrgVDCResponseDict = xmltodict.parse(sourceOrgVDCResponse.content)

            # sourceStorageProfileIDsList holds list the IDs of the source org vdc storage profiles
            sourceStorageProfileIDsList = []
            # sourceStorageProfilesList holds the list of dictionaries of details of each source org vdc storage profile
            sourceStorageProfilesList = []
            storageProfiles = sourceOrgVDCResponseDict['AdminVdc']['VdcStorageProfiles'][
                'VdcStorageProfile'] if isinstance(
                sourceOrgVDCResponseDict['AdminVdc']['VdcStorageProfiles']['VdcStorageProfile'], list) else [
                sourceOrgVDCResponseDict['AdminVdc']['VdcStorageProfiles']['VdcStorageProfile']]
            for storageProfile in storageProfiles:
                sourceStorageProfilesList.append(storageProfile)
                sourceStorageProfileIDsList.append(storageProfile['@id'])

            # get api call to retrieve the organization details
            orgResponse = self.restClientObj.get(orgUrl, headers=self.headers)
            orgResponseDict = xmltodict.parse(orgResponse.content)
            # retrieving the organization ID
            orgId = orgResponseDict['AdminOrg']['@id'].split(':')[-1]

            # if no catalogs exist
            if not orgResponseDict['AdminOrg'].get("Catalogs"):
                logger.debug("No Catalogs exist in Organization")
                return

            # orgCatalogs contains list of all catalogs in the organization
            # each org catalog in orgCatalogs is of type dict which has keys {'@href', '@name', '@type'}
            orgCatalogs = orgResponseDict['AdminOrg']["Catalogs"]["CatalogReference"] if isinstance(
                orgResponseDict['AdminOrg']["Catalogs"]["CatalogReference"], list) else [
                orgResponseDict['AdminOrg']["Catalogs"]["CatalogReference"]]

            # sourceOrgVDCCatalogDetails will hold list of only catalogs present in the source org vdc
            sourceOrgVDCCatalogDetails = []
            # iterating over all the organization catalogs
            for catalog in orgCatalogs:
                # get api call to retrieve the catalog details
                catalogResponse = self.restClientObj.get(catalog['@href'], headers=self.headers)
                catalogResponseDict = xmltodict.parse(catalogResponse.content)
                if catalogResponseDict['AdminCatalog'].get('CatalogStorageProfiles'):
                    # checking if catalogs storage profile is same from source org vdc storage profile by matching the ID of storage profile
                    if catalogResponseDict['AdminCatalog']['CatalogStorageProfiles']['VdcStorageProfile'][
                        '@id'] in sourceStorageProfileIDsList:
                        # creating the list of catalogs from source org vdc
                        sourceOrgVDCCatalogDetails.append(catalogResponseDict['AdminCatalog'])
                else:
                    # skipping the organization level catalogs(i.e catalogs that doesnot belong to any org vdc) while are handled in the for-else loop
                    logger.debug("Skipping the catalog '{}' since catalog doesnot belong to any org vdc".format(
                        catalog['@name']))

            # getting the target storage profile details
            targetOrgVDCId = targetOrgVDCId.split(':')[-1]
            # url to get target org vdc details
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_BY_ID.format(targetOrgVDCId))

            # get api call to retrieve the target org vdc details
            targetOrgVDCResponse = self.restClientObj.get(url, self.headers)
            targetOrgVDCResponseDict = xmltodict.parse(targetOrgVDCResponse.content)
            # retrieving target org vdc storage profiles list
            targetOrgVDCStorageList = targetOrgVDCResponseDict['AdminVdc']['VdcStorageProfiles'][
                'VdcStorageProfile'] if isinstance(
                targetOrgVDCResponseDict['AdminVdc']['VdcStorageProfiles']['VdcStorageProfile'], list) else [
                targetOrgVDCResponseDict['AdminVdc']['VdcStorageProfiles']['VdcStorageProfile']]

            # iterating over the source org vdc catalogs to migrate them to target org vdc
            for srcCatalog in sourceOrgVDCCatalogDetails:
                logger.debug("Migrating source Org VDC specific Catalogs")
                storageProfileHref = ''
                for storageProfile in targetOrgVDCStorageList:
                    srcOrgVDCStorageProfileDetails = self.getOrgVDCStorageProfileDetails(
                        srcCatalog['CatalogStorageProfiles']['VdcStorageProfile']['@id'])
                    # checking for the same name of target org vdc profile name matching with source catalog's storage profile
                    if srcOrgVDCStorageProfileDetails['AdminVdcStorageProfile']['@name'] == storageProfile['@name']:
                        storageProfileHref = storageProfile['@href']
                        break

                # creating target catalogs for migration
                payloadDict = {'catalogName': srcCatalog['@name'] + '-t',
                               'storageProfileHref': storageProfileHref,
                               'catalogDescription': srcCatalog['Description'] if srcCatalog.get('Description') else ''}
                catalogId = self.createCatalog(payloadDict, orgId)

                if catalogId:
                    # empty catalogs
                    if not srcCatalog.get('CatalogItems'):
                        logger.debug("Migrating empty catalog '{}'".format(srcCatalog['@name']))
                        # deleting the source org vdc catalog
                        self.deleteSourceCatalog(srcCatalog['@href'], srcCatalog)
                        # renaming the target org vdc catalog
                        self.renameTargetCatalog(catalogId, srcCatalog)
                        continue

                    # non-empty catalogs
                    logger.debug("Migrating non-empty catalog '{}'".format(srcCatalog['@name']))
                    # retrieving the catalog items of the catalog
                    catalogItemList = srcCatalog['CatalogItems']['CatalogItem'] if isinstance(srcCatalog['CatalogItems']['CatalogItem'], list) else [srcCatalog['CatalogItems']['CatalogItem']]

                    vAppTemplateCatalogItemList = []
                    mediaCatalogItemList = []
                    # creating seperate lists for catalog items - 1. One for media catalog items 2. One for vApp template catalog items
                    for catalogItem in catalogItemList:
                        catalogItemResponse = self.restClientObj.get(catalogItem['@href'], headers=self.headers)
                        catalogItemResponseDict = xmltodict.parse(catalogItemResponse.content)
                        if catalogItemResponseDict['CatalogItem']['Entity']['@type'] == vcdConstants.TYPE_VAPP_TEMPLATE:
                            vAppTemplateCatalogItemList.append(catalogItem)
                        elif catalogItemResponseDict['CatalogItem']['Entity']['@type'] == vcdConstants.TYPE_VAPP_MEDIA:
                            mediaCatalogItemList.append(catalogItem)
                        else:
                            raise Exception("Catalog Item '{}' of type '{}' is not supported".format(catalogItem['@name'], catalogItemResponseDict['CatalogItem']['Entity']['@type']))

                    logger.debug('Starting to move source org VDC catalog items: ')
                    # Note: First migrating the media then migrating the vapp templates to target catalog(because if migrating of media fails(it fails if the same media is used by other org vdc as well) then no need of remigrating back the vapp templates to source catalogs)
                    # moving each catalog item from the 'mediaCatalogItemList' to target catalog created above
                    for catalogItem in mediaCatalogItemList:
                        logger.debug("Migrating Media catalog item: '{}'".format(catalogItem['@name']))
                        # creating payload data to move media
                        payloadDict = {'catalogItemName': catalogItem['@name'],
                                       'catalogItemHref': catalogItem['@href']}
                        self.moveCatalogItem(payloadDict, catalogId)

                    # moving each catalog item from the 'vAppTemplateCatalogItemList' to target catalog created above
                    for catalogItem in vAppTemplateCatalogItemList:
                        logger.debug("Migrating vApp Template catalog item: '{}'".format(catalogItem['@name']))
                        # creating payload data to move vapp template
                        payloadDict = {'catalogItemName': catalogItem['@name'],
                                       'catalogItemHref': catalogItem['@href']}
                        self.moveCatalogItem(payloadDict, catalogId)

                    # deleting the source org vdc catalog
                    self.deleteSourceCatalog(srcCatalog['@href'], srcCatalog)
                    # renaming the target org vdc catalog
                    self.renameTargetCatalog(catalogId, srcCatalog)

                    # deleting the temporary lists
                    del vAppTemplateCatalogItemList
                    del mediaCatalogItemList
            else:
                # migrating non-specific org vdc  catalogs
                # in this case catalog uses any storage available in the organization; but while creating media or vapp template it uses our source org vdc's storage profile by default
                logger.debug("Migrating Non-specific Org VDC Catalogs")

                # case where no catalog items found in source org vdc to migrate non-specific org vdc catalog
                if sourceOrgVDCResponseDict['AdminVdc']['ResourceEntities'] is None:
                    # no catalog items found in the source org vdc
                    logger.debug("No catalogs items found in the source org vdc")
                    return

                # resourceEntitiesList holds the resource entities of source org vdc
                resourceEntitiesList = sourceOrgVDCResponseDict['AdminVdc']['ResourceEntities']['ResourceEntity'] if isinstance(sourceOrgVDCResponseDict['AdminVdc']['ResourceEntities']['ResourceEntity'], list) else [sourceOrgVDCResponseDict['AdminVdc']['ResourceEntities']['ResourceEntity']]

                # sourceCatalogItemsList holds the list of resource entities of type media or vapp template found in source org vdc
                # each catalog item in sourceCatalogItemsList after updating will be dictionary with keys { '@href', '@id', '@name', '@type', 'catalogName', 'catalogHref', 'catalogItemHref', 'catalogDescription'}
                sourceCatalogItemsList = [resourceEntity for resourceEntity in resourceEntitiesList if resourceEntity['@type'] == vcdConstants.TYPE_VAPP_MEDIA or resourceEntity['@type'] == vcdConstants.TYPE_VAPP_TEMPLATE]

                organizationCatalogItemList = []
                # organizationCatalogItemList holds the resource entities of type vapp template from whole organization
                organizationCatalogItemList = self.getvAppTemplates(orgId)
                # now organizationCatalogItemList will also hold resource entities of type media from whole organization
                organizationCatalogItemList.extend(self.getCatalogMedia(orgId))

                # commonCatalogItemsDetailsList holds the details of catalog common from source org vdc and organization
                # commonCatalogItemsDetailsList will have many keys, but our interest keys are 'href' - href, 'catalogName' - name of catalog, 'catalog' - catalog href, 'catalogItem' - catalog item href
                # Note: If the catalog item is of media type then all above keys are present; but if catalog item is of type vapp template then only  'href' and 'catalogName' is present.
                # So we need to find the values of 'catalog' and 'catalogItem', since those are needed further
                commonCatalogItemsDetailsList = [orgResource for orgResource in organizationCatalogItemList for srcResource in sourceCatalogItemsList if srcResource['@href'] == orgResource['href']]

                # getting the default storage profile of the target org vdc
                defaultTargetStorageProfileHref = None
                # iterating over the target org vdc storage profiles
                for eachStorageProfile in targetOrgVDCStorageList:
                    # fetching the details of the storage profile
                    orgVDCStorageProfileDetails = self.getOrgVDCStorageProfileDetails(eachStorageProfile['@id'])
                    # checking if the storage profile is the default one
                    if orgVDCStorageProfileDetails['AdminVdcStorageProfile']['Default'] == "true":
                        defaultTargetStorageProfileHref = eachStorageProfile['@href']
                        break

                # catalogItemDetailsList is a list of dictionaries; each dictionary holds the details of each catalog item found in source org vdc
                # each dictionary finally holds keys {'@href', '@id', '@name', '@type', 'catalogName', 'catalogHref', 'catalogItemHref', 'catalogDescription'}
                catalogItemDetailsList = []
                # catalogNameList is a temporary list used to get the single occurence of catalog in catalogDetailsList list
                catalogNameList = []
                # catalogDetailsList is a list of dictionaries; each dictionary holds the details of each catalog
                # each dictionary finally holds keys {'catalogName', 'catalogHref', 'catalogDescription'}
                catalogDetailsList = []
                # iterating over the source catalog items
                for eachResource in sourceCatalogItemsList:
                    # iterating over the catalogs items found in both source org vdc and organization
                    for resource in commonCatalogItemsDetailsList:
                        if eachResource['@href'] == resource['href']:
                            # catalogItem is a dict to hold the catalog item details
                            catalogItem = eachResource
                            catalogItem['catalogName'] = resource['catalogName']

                            for orgCatalog in orgCatalogs:
                                if orgCatalog['@name'] == resource['catalogName']:
                                    catalogItem['catalogHref'] = orgCatalog['@href']
                                    catalogResponseDict = self.getCatalogDetails(orgCatalog['@href'])
                                    if catalogResponseDict.get('catalogItems'):
                                        catalogItemsList = catalogResponseDict['catalogItems']['catalogItem'] if isinstance(catalogResponseDict['catalogItems']['catalogItem'], list) else [catalogResponseDict['catalogItems']['catalogItem']]
                                        for item in catalogItemsList:
                                            if item['name'] == eachResource['@name']:
                                                catalogItem['catalogItemHref'] = item['href']
                                                break

                            catalogResponseDict = self.getCatalogDetails(catalogItem['catalogHref'])
                            catalogItem['catalogDescription'] = catalogResponseDict['description'] if catalogResponseDict.get('description') else ''
                            catalogItemDetailsList.append(catalogItem)
                            if resource['catalogName'] not in catalogNameList:
                                catalogNameList.append(resource['catalogName'])
                                catalog = {'catalogName': resource['catalogName'],
                                           'catalogHref': catalogItem['catalogHref'],
                                           'catalogDescription': catalogResponseDict['description'] if catalogResponseDict.get('description') else ''}
                                catalogDetailsList.append(catalog)
                # deleting the temporary list since no more needed
                del catalogNameList

                # iterating over catalogs in catalogDetailsList
                for catalog in catalogDetailsList:
                    # creating the payload dict to create a place holder target catalog
                    payloadDict = {'catalogName': catalog['catalogName'] + '-t',
                                   'storageProfileHref': defaultTargetStorageProfileHref,
                                   'catalogDescription': catalog['catalogDescription']}
                    # create api call to create a new place holder catalog
                    catalogId = self.createCatalog(payloadDict, orgId)
                    if catalogId:

                        vAppTemplateCatalogItemList = []
                        mediaCatalogItemList = []
                        # creating seperate lists for catalog items - 1. One for media catalog items 2. One for vApp template catalog items
                        for catalogItem in catalogItemDetailsList:
                            if catalogItem['@type'] == vcdConstants.TYPE_VAPP_TEMPLATE:
                                vAppTemplateCatalogItemList.append(catalogItem)
                            elif catalogItem['@type'] == vcdConstants.TYPE_VAPP_MEDIA:
                                mediaCatalogItemList.append(catalogItem)
                            else:
                                raise Exception("Catalog Item '{}' of type '{}' is not supported".format(catalogItem['@name'], catalogItem['@type']))

                        logger.debug('Starting to move non-specific org VDC catalog items: ')
                        # iterating over the catalog items in mediaCatalogItemList
                        for catalogItem in mediaCatalogItemList:
                            # checking if the catalogItem belongs to the above created catalog; if so migrating that catalogItem to the newly created target catalog
                            if catalogItem['catalogName'] == catalog['catalogName']:
                                logger.debug("Migrating Media catalog item: '{}'".format(catalogItem['@name']))
                                # migrating this catalog item
                                payloadDict = {'catalogItemName': catalogItem['@name'],
                                               'catalogItemHref': catalogItem['catalogItemHref']}
                                # move api call to migrate the catalog item
                                self.moveCatalogItem(payloadDict, catalogId)

                        # iterating over the catalog items in mediaCatalogItemList
                        for catalogItem in vAppTemplateCatalogItemList:
                            # checking if the catalogItem belongs to the above created catalog; if so migrating that catalogItem to the newly created target catalog
                            if catalogItem['catalogName'] == catalog['catalogName']:
                                logger.debug("Migrating vApp Template catalog item: '{}'".format(catalogItem['@name']))
                                # migrating this catalog item
                                payloadDict = {'catalogItemName': catalogItem['@name'],
                                               'catalogItemHref': catalogItem['catalogItemHref']}
                                # move api call to migrate the catalog item
                                self.moveCatalogItem(payloadDict, catalogId)

                        catalogData = {'@name': catalog['catalogName'],
                                       '@href': catalog['catalogHref'],
                                       'Description': catalog['catalogDescription']}

                        # deleting the source org vdc catalog
                        self.deleteSourceCatalog(catalogData['@href'], catalogData)
                        # renaming the target org vdc catalog
                        self.renameTargetCatalog(catalogId, catalogData)

                        # deleting the temporary lists
                        del vAppTemplateCatalogItemList
                        del mediaCatalogItemList

        except Exception:
            raise

    @isSessionExpired
    def getSourceEdgeGatewayMacAddress(self, interfacesList):
        """
        Description :   Get source edge gateway mac address for source org vdc network portgroups
        Parameters  :   portGroupList   -   source org vdc networks corresponding portgroup details (LIST)
                        interfacesList  -   Interfaces details of source edge gateway (LIST)
        Returns     :   macAddressList  -   list of mac addresses (LIST)
        """
        try:
            data = self.rollback.apiData
            portGroupList = data.get('portGroupList')
            logger.debug("Getting Source Edge Gateway Mac Address")
            macAddressList = []
            for portGroup in portGroupList:
                for nicDetail in interfacesList:
                    # comparing source org vdc network portgroup moref and edge gateway interface details
                    if portGroup['moref'] == nicDetail['value']['backing']['network']:
                        macAddressList.append(nicDetail['value']['mac_address'])
            return macAddressList
        except Exception:
            raise

    def checkIfSourceVappsExist(self, orgVDCId):
        """
        Description :   Checks if there exist atleast a single vapp in source org vdc
        Returns     :   True    -   if found atleast single vapp (BOOL)
                        False   -   if not a single vapp found in source org vdc (BOOL)
        """
        try:
            orgvdcId = orgVDCId.split(':')[-1]
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_BY_ID.format(orgvdcId))
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
            else:
                raise Exception('Error occurred while retrieving Org VDC - {} details'.format(orgVDCId))
            if not responseDict['AdminVdc'].get('ResourceEntities'):
                logger.debug('No resource entities found in source Org VDC')
                return False
            # getting list instance of resources in the source org vdc
            sourceOrgVDCEntityList = responseDict['AdminVdc']['ResourceEntities']['ResourceEntity'] if isinstance(
                responseDict['AdminVdc']['ResourceEntities']['ResourceEntity'], list) else [
                responseDict['AdminVdc']['ResourceEntities']['ResourceEntity']]
            vAppList = [vAppEntity for vAppEntity in sourceOrgVDCEntityList if vAppEntity['@type'] == vcdConstants.TYPE_VAPP]
            if len(vAppList) >= 1:
                return True
            return False
        except Exception:
            raise

    def migrateVapps(self, sourceOrgVDCName, metadata, timeout=None):
        """
        Description : Migrating vApps i.e composing target placeholder vapps and recomposing target vapps
        Parameters  : sourceOrgVDCName  -   Name of source Org VDC name (STRING)
                      metadata  -  metadata to check status of move vapp  (DICT)
                      timeout  -  timeout to be used for vapp migration task (INT)
        """
        try:
            self.sourceOrgVDCName = sourceOrgVDCName
            sourceOrgVDCId = self.rollback.apiData['sourceOrgVDC']['@id']
            targetOrgVDCId = self.rollback.apiData['targetOrgVDC']['@id']
            dfwStatus = True if self.rollback.apiData.get('OrgVDCGroupID') else False
            orgVDCNetworkList = self.getOrgVDCNetworks(targetOrgVDCId, 'targetOrgVDCNetworks', dfwStatus=dfwStatus, saveResponse=False)
            # handling the case if there exist no vapps in source org vdc
            # if no source vapps are present then skipping all the below steps as those are not required
            if not self.checkIfSourceVappsExist(sourceOrgVDCId):
                logger.debug("No Vapps in Source Org VDC, hence skipping migrateVapps task.")
                self.rollback.executionResult['moveVapp'] = True
                self.rollback.executionResult['enableTargetAffinityRules'] = True
            else:
                # Logging continuation message
                if self.rollback.metadata and not hasattr(self.rollback, 'retry'):
                    logger.info(
                        'Continuing migration of NSX-V backed Org VDC to NSX-T backed from {}.'.format(
                            "Migration of vApps"))
                    self.rollback.retry = True

                if not metadata.get('moveVapp'):
                    # recompose target vApp by adding source vm
                    logger.info('Migrating source vApps.')
                    self.moveVapp(sourceOrgVDCId, targetOrgVDCId, orgVDCNetworkList, timeout)
                    logger.info('Successfully migrated source vApps.')

            # configuring Affinity rules
            self.enableTargetAffinityRules()
        except Exception:
            raise

    @isSessionExpired
    def getEdgeVmId(self):
        """
        Description : Method to get edge VM ID
        Parameters : edgeGatewayId - Edge gateway ID (STRING)
        Returns : edgeVmId - Edge Gateway VM ID (STRING)
        """
        try:
            logger.debug("Getting Edge VM ID")
            edgeVmIdList = []
            edgeGatewayIdList = self.rollback.apiData['sourceEdgeGatewayId']
            for edgeGatewayId in edgeGatewayIdList:
                orgVDCEdgeGatewayId = edgeGatewayId.split(':')[-1]
                # url to retrieve the firewall config details of edge gateway
                url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                      vcdConstants.NETWORK_EDGES,
                                      vcdConstants.EDGE_GATEWAY_STATUS.format(orgVDCEdgeGatewayId))
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    # Convert XML data to dictionary
                    edgeNetworkDict = xmltodict.parse(response.content)
                    # Get the edge gateway VM ID
                    # if edge ha is configured, then the response is list
                    if isinstance(edgeNetworkDict[vcdConstants.EDGE_GATEWAY_STATUS_KEY][
                                      vcdConstants.EDGE_GATEWAY_VM_STATUS_KEY][vcdConstants.EDGE_GATEWAY_VM_STATUS_KEY],
                                  list):
                        edgeVmId = [edgeNetworkData for edgeNetworkData in
                                    edgeNetworkDict[vcdConstants.EDGE_GATEWAY_STATUS_KEY][
                                        vcdConstants.EDGE_GATEWAY_VM_STATUS_KEY][
                                        vcdConstants.EDGE_GATEWAY_VM_STATUS_KEY] if
                                    edgeNetworkData['haState'] == 'active']
                        if edgeVmId:
                            edgeVmId = edgeVmId[0]["id"]
                        else:
                            raise Exception(
                                'Could not find the edge vm id for source edge gateway {}'.format(edgeGatewayId))
                    else:
                        edgeVmId = \
                        edgeNetworkDict[vcdConstants.EDGE_GATEWAY_STATUS_KEY][vcdConstants.EDGE_GATEWAY_VM_STATUS_KEY][
                            vcdConstants.EDGE_GATEWAY_VM_STATUS_KEY]["id"]
                    edgeVmIdList.append(edgeVmId)
                else:
                    errorDict = xmltodict.parse(response.content)
                    raise Exception(
                        "Failed to get edge gateway status. Error - {}".format(errorDict['error']['details']))
            return edgeVmIdList
        except Exception:
            raise

    @description("connection of dummy uplink to source Edge gateway")
    @remediate
    def connectUplinkSourceEdgeGateway(self, sourceEdgeGatewayIdList, rollback=False):
        """
        Description :  Connect another uplink to source Edge Gateways from the specified OrgVDC
        Parameters  :   sourceEdgeGatewayId -   Id of the Organization VDC Edge gateway (STRING)
                        rollback - key that decides whether to perform rollback or not (BOOLEAN)
        """
        try:
            if rollback:
                logger.info('Rollback: Disconnecting dummy-uplink from source Edge Gateway')
            else:
                logger.info('Connecting dummy uplink to source Edge gateway.')
            logger.debug("Connecting another uplink to source Edge Gateway")

            data = self.rollback.apiData
            dummyExternalNetwork = self.getExternalNetwork(data['dummyExternalNetwork']['name'], isDummyNetwork=True)

            if not rollback:
                # Validating if sufficient free IP's are present in dummy external network
                freeIpCount = dummyExternalNetwork['totalIpCount'] - dummyExternalNetwork['usedIpCount']
                if freeIpCount < len(sourceEdgeGatewayIdList):
                    raise Exception(
                        f"{len(sourceEdgeGatewayIdList)} free IP's are required in dummy external network "
                        f"but only {freeIpCount} free IP's are present.")

            for sourceEdgeGatewayId in sourceEdgeGatewayIdList:
                orgVDCEdgeGatewayId = sourceEdgeGatewayId.split(':')[-1]
                # url to connect uplink the source edge gateway
                url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                    vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(orgVDCEdgeGatewayId))
                acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
                headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
                # retrieving the details of the edge gateway
                response = self.restClientObj.get(url, headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    gatewayInterfaces = responseDict['configuration']['gatewayInterfaces']['gatewayInterface']
                    if len(gatewayInterfaces) >= 9:
                        raise Exception(
                            'No more uplinks present on source Edge Gateway to connect dummy External Uplink.')
                    if not rollback:
                        dummyUplinkAlreadyConnected = True if [interface for interface in gatewayInterfaces
                                                               if interface['name'] == dummyExternalNetwork['name']] \
                                                                else False
                        if dummyUplinkAlreadyConnected:
                            logger.debug("Dummy Uplink is already connected to edge gateway - {}".format(responseDict['name']))
                            continue
                        filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.json')
                        # creating the dummy external network link
                        networkId = dummyExternalNetwork['id'].split(':')[-1]
                        networkHref = "{}network/{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                                            networkId)
                        # creating the payload data for adding dummy external network
                        payloadDict = {'edgeGatewayUplinkName': dummyExternalNetwork['name'],
                                       'networkHref': networkHref,
                                       'uplinkGateway': dummyExternalNetwork['subnets']['values'][0]['gateway'],
                                       'prefixLength': dummyExternalNetwork['subnets']['values'][0]['prefixLength'],
                                       'uplinkIpAddress': ""}
                        payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='json',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.CONNECT_ADDITIONAL_UPLINK_EDGE_GATEWAY_TEMPLATE)
                        payloadData = json.loads(payloadData)
                        gatewayInterfaces.append(payloadData)
                    else:
                        # Computation to remove dummy external network key from API payload
                        for index, value in enumerate(gatewayInterfaces):
                            if value['name'] == dummyExternalNetwork['name']:
                                gatewayInterfaces.pop(index)
                    responseDict['configuration']['gatewayInterfaces']['gatewayInterface'] = gatewayInterfaces
                    responseDict['edgeGatewayServiceConfiguration'] = None
                    del responseDict['tasks']
                    payloadData = json.dumps(responseDict)
                    acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
                    self.headers["Content-Type"] = vcdConstants.XML_UPDATE_EDGE_GATEWAY
                    headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader,
                               'Content-Type': vcdConstants.JSON_UPDATE_EDGE_GATEWAY}
                    # updating the details of the edge gateway
                    response = self.restClientObj.put(url + '/action/updateProperties', headers, data=payloadData)
                    responseData = response.json()
                    if response.status_code == requests.codes.accepted:
                        taskUrl = responseData["href"]
                        if taskUrl:
                            # checking the status of renaming target org vdc task
                            self._checkTaskStatus(taskUrl=taskUrl)
                            if rollback:
                                logger.debug(
                                    'Disconnected dummy uplink from source Edge gateway {} successfully'.format(
                                        responseDict['name']))
                            else:
                                logger.debug('Connected dummy uplink to source Edge gateway {} successfully'.format(
                                    responseDict['name']))

                                # Saving rollback key after successful dummy uplink connection to one edge gateway
                                self.rollback.key = 'connectUplinkSourceEdgeGateway'
                            continue
                    else:
                        if rollback:
                            raise Exception(
                                "Failed to disconnect dummy uplink from source Edge gateway {} with error {}".format(
                                    responseDict['name'], responseData['message']))
                        else:
                            raise Exception(
                                "Failed to connect dummy uplink to source Edge gateway {} with error {}".format(
                                    responseDict['name'], responseData['message']))
            if not rollback:
                logger.info('Successfully connected dummy uplink to source Edge gateway.')
        except Exception:
            # Saving rollback key in case of failure
            orgVDCId = self.rollback.apiData.get('sourceOrgVDC', {}).get('@id')
            self.createMetaDataInOrgVDC(orgVDCId,
                                        metadataDict={'rollbackKey': self.rollback.key}, domain='system')
            raise

    @isSessionExpired
    def updateSourceExternalNetwork(self, networkName, edgeGatewaySubnetDict):
        """
        Description : Update Source External Network sub allocated ip pools
        Parameters : networkName: source external network name (STRING)
                     edgeGatewaySubnetDict: source edge gateway sub allocated ip pools (DICT)
        """
        try:
            # url to get all the external networks
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EXTERNAL_NETWORKS)
            # get api call to get all the external networks
            getResponse = self.restClientObj.get(url, self.headers)
            responseDict = getResponse.json()
            if getResponse.status_code == requests.codes.ok:
                # iterating over all the external networks
                for response in responseDict['values']:
                    # checking if networkName is present in the list,
                    if response['name'] == networkName:
                        # getting the external network sub allocated pools
                        for index, subnet in enumerate(response['subnets']['values']):
                            externalRanges = subnet['ipRanges']['values']
                            externalRangeList = []
                            # creating range of source external network pool range
                            for externalRange in externalRanges:
                                externalRangeList.extend(
                                    self.createIpRange(externalRange['startAddress'], externalRange['endAddress']))
                            externalNetworkSubnet = ipaddress.ip_network(
                                '{}/{}'.format(subnet['gateway'], subnet['prefixLength']),
                                strict=False)
                            subIpPools = edgeGatewaySubnetDict.get(externalNetworkSubnet)
                            # If no ipPools are used from corresponding network then skip the iteration
                            if not subIpPools:
                                continue
                            # creating range of source edge gateway sub allocated pool range
                            subIpRangeList = []
                            for ipRange in subIpPools:
                                subIpRangeList.extend(
                                    self.createIpRange(ipRange['startAddress'], ipRange['endAddress']))
                            # removing the sub allocated ip pools of source edge gateway from source external network
                            for ip in subIpRangeList:
                                if ip in externalRangeList:
                                    externalRangeList.remove(ip)
                            # getting the source edge gateway sub allocated ip pool after removing used ips i.e source edge gateway
                            result = self.createExternalNetworkSubPoolRangePayload(externalRangeList)
                            response['subnets']['values'][index]['ipRanges']['values'] = result

                        # API call to update external network details
                        payloadData = json.dumps(response)
                        payloadData = json.loads(payloadData)
                        payloadData = json.dumps(payloadData)
                        url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                               vcdConstants.ALL_EXTERNAL_NETWORKS, response['id'])
                        # put api call to update the external networks ip allocation
                        self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                        apiResponse = self.restClientObj.put(url, self.headers, data=payloadData)
                        if apiResponse.status_code == requests.codes.accepted:
                            taskUrl = apiResponse.headers['Location']
                            # checking the status of the creating org vdc network task
                            self._checkTaskStatus(taskUrl=taskUrl)
                            logger.debug('Updating external network sub allocated ip pool {}'.format(networkName))
            else:
                raise Exception('Failed to fetch external network details')
        except Exception:
            raise

    @staticmethod
    def createExternalNetworkSubPoolRangePayload(externalNetworkPoolRangeList):
        """
        Description : Create external network sub ip pool range payload
        Parameters : externalNetworkPoolRangeList - external network pool range (LIST)
        """
        resultData = []
        total = 1
        for ipAddress in externalNetworkPoolRangeList:
            startAddress = ipAddress
            if total == len(externalNetworkPoolRangeList):
                nextAddress = startAddress
            else:
                nextAddress = externalNetworkPoolRangeList[total]
            start = list(map(int, startAddress.split('.')))
            next = list(map(int, nextAddress.split('.')))
            if start[3] + 1 == next[3]:
                resultData.append({'startAddress': startAddress, 'endAddress': nextAddress})
            else:
                resultData.append({'startAddress': startAddress, 'endAddress': startAddress})
            total += 1
        return resultData

    @isSessionExpired
    def deleteSourceCatalog(self, catalogUrl, srcCatalog):
        """
        Description :   Deletes the source org vdc catalog of the specified catalog url
        Parameters  :   catalogUrl  -   url of the source catalog (STRING)
                        srcCatalog  -   Details of the source catalog (DICT)
        """
        try:
            # deleting catalog
            logger.debug("Deleting catalog '{}'".format(srcCatalog['@name']))
            # url to delete the catalog
            deleteCatalogUrl = '{}?recursive=true&force=true'.format(catalogUrl)
            # delete api call to delete the catalog
            deleteCatalogResponse = self.restClientObj.delete(deleteCatalogUrl, self.headers)
            deleteCatalogResponseDict = xmltodict.parse(deleteCatalogResponse.content)
            if deleteCatalogResponse.status_code == requests.codes.accepted:
                task = deleteCatalogResponseDict["Task"]
                taskUrl = task["@href"]
                if taskUrl:
                    # checking the status of deleting the catalog task
                    self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug("Catalog '{}' deleted successfully".format(srcCatalog['@name']))
            else:
                raise Exception("Failed to delete catalog '{}' - {}".format(srcCatalog['@name'],
                                                                            deleteCatalogResponseDict['Error'][
                                                                                '@message']))

        except Exception:
            raise

    @isSessionExpired
    def renameTargetCatalog(self, catalogId, srcCatalog):
        """
        Description :   Renames the target org vdc catalog of the specified catalog url
        Parameters  :   catalogId   -   ID of the source catalog (STRING)
                        srcCatalog  -   Details of the source catalog (DICT)
        """
        try:
            # renaming catalog
            logger.debug("Renaming the catalog '{}' to '{}'".format(srcCatalog['@name'] + '-t',
                                                                    srcCatalog['@name']))
            # url to rename the catalog
            renameCatalogUrl = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                             vcdConstants.RENAME_CATALOG.format(catalogId))
            # creating the payload
            payloadDict = {'catalogName': srcCatalog['@name'],
                           'catalogDescription': srcCatalog['Description'] if srcCatalog.get('Description') else ''}

            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
            payloadData = self.vcdUtils.createPayload(filePath,
                                                      payloadDict,
                                                      fileType='yaml',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.RENAME_CATALOG_TEMPLATE)
            payloadData = json.loads(payloadData)
            # setting the content-type to rename the catalog
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.VCD_API_HEADER,
                       'Content-Type': vcdConstants.RENAME_CATALOG_CONTENT_TYPE}
            # put api call to rename the catalog back to its original name
            renameCatalogResponse = self.restClientObj.put(renameCatalogUrl, headers, data=payloadData)
            if renameCatalogResponse.status_code == requests.codes.ok:
                logger.debug("Catalog '{}' renamed to '{}' successfully".format(srcCatalog['@name'] + '-t',
                                                                                srcCatalog['@name']))
            else:
                raise Exception("Failed to rename catalog '{}' to '{}'".format(srcCatalog['@name'] + '-t',
                                                                               srcCatalog['@name']))
        except Exception:
            raise

    @isSessionExpired
    def moveVappApiCall(self, vApp, targetOrgVDCNetworkList, targetOrgVDCId, filePath, timeout, rollback=False):
        """
            Description :   Prepares the payload for moving the vApp and sends post api call for it
            Parameters  :   vApp  -   Information related to a specific vApp (DICT)
                            targetOrgVDCNetworkList - All the target org vdc networks (LIST)
                            targetOrgVDCId - ID of target org vdc (STRING)
                            filePath - file path of template.yml which holds all the templates (STRING)
                            timeout  -  timeout to be used for vapp migration task (INT)
                            rollback - whether to rollback from T2V (BOOLEAN)
        """
        otherNetworkList = list()
        if rollback:
            logger.info('Moving vApp - {} to source Org VDC'.format(vApp['@name']))
        else:
            logger.info('Moving vApp - {} to target Org VDC - {}'.format(vApp['@name'], self.sourceOrgVDCName + '-t'))
        networkList = []
        response = self.restClientObj.get(vApp['@href'], self.headers)
        responseDict = xmltodict.parse(response.content)
        vAppData = responseDict['VApp']
        # checking for the 'NetworkConfig' in 'NetworkConfigSection' of vapp
        if vAppData['NetworkConfigSection'].get('NetworkConfig'):
            vAppNetworkList = vAppData['NetworkConfigSection']['NetworkConfig'] \
                if isinstance(vAppData['NetworkConfigSection']['NetworkConfig'], list) else [
                vAppData['NetworkConfigSection']['NetworkConfig']]
            if rollback:
                # retrieving the network details list of same name networks from source & target, target networks will have -v2t appended
                networkList = [(network, vAppNetwork) for network in targetOrgVDCNetworkList for vAppNetwork in
                               vAppNetworkList if vAppNetwork['@networkName'] == network['name'] + '-v2t']
            else:
                # retrieving the network details list of same name networks from source & target
                networkList = [(network, vAppNetwork) for network in targetOrgVDCNetworkList for vAppNetwork in
                               vAppNetworkList if vAppNetwork['@networkName'] + '-v2t' == network['name']]
            # retrieving the network details of other networks other than org vdc networks
            otherNetworkList = [vAppNetwork for vAppNetwork in vAppNetworkList]
        # if networks present
        networkPayloadData = ''
        if networkList:
            # iterating over the network list
            for network, vAppNetwork in networkList:
                # creating payload dictionary with network details
                networkName = "{}network/{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                                    network["id"].split(':')[-1])
                payloadDict = {'networkName': network['name'],
                               'networkDescription': vAppNetwork['Description'] if vAppNetwork.get(
                                   'Description') else '',
                               'parentNetwork': networkName,
                               'fenceMode': vAppNetwork['Configuration']['FenceMode']}
                payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                          componentName=vcdConstants.COMPONENT_NAME,
                                                          templateName=vcdConstants.MOVE_VAPP_NETWORK_CONFIG_TEMPLATE)
                networkPayloadData += payloadData.strip("\"")
        # creating payload for no network and vapp network
        if otherNetworkList:
            for network in otherNetworkList:
                if not network['Configuration'].get('ParentNetwork'):
                    if network['@networkName'] == 'none':
                        networkName = network['@networkName']
                    else:
                        if rollback:
                            networkName = network['@networkName'].replace('-v2t', '')
                        else:
                            networkName = network['@networkName'] + '-v2t'
                    # if static ip pools exist in vapp network
                    if network['Configuration']['IpScopes']['IpScope'].get('IpRanges'):
                        payloadDict = {'networkName': networkName,
                                       'networkDescription': network['Description'] if network.get(
                                           'Description') else '',
                                       'fenceMode': network['Configuration']['FenceMode'],
                                       'isInherited': network['Configuration']['IpScopes']['IpScope']['IsInherited'],
                                       'gateway': network['Configuration']['IpScopes']['IpScope']['Gateway'],
                                       'netmask': network['Configuration']['IpScopes']['IpScope']['Netmask'],
                                       'subnet': network['Configuration']['IpScopes']['IpScope'][
                                           'SubnetPrefixLength'] if
                                       network['Configuration']['IpScopes']['IpScope'].get('SubnetPrefixLength') else 1,
                                       'dns1': network['Configuration']['IpScopes']['IpScope']['Dns1'] if
                                       network['Configuration']['IpScopes']['IpScope'].get('Dns1') else '',
                                       'startAddress':
                                           network['Configuration']['IpScopes']['IpScope']['IpRanges']['IpRange'][
                                               'StartAddress'],
                                       'endAddress':
                                           network['Configuration']['IpScopes']['IpScope']['IpRanges']['IpRange'][
                                               'EndAddress'],
                                       'isDeployed': network['IsDeployed']}
                        payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.MOVE_VAPP_NO_NETWORK_IP_POOL_CONFIG_TEMPLATE)
                    else:
                        payloadDict = {'networkName': networkName,
                                       'networkDescription': network['Description'] if network.get(
                                           'Description') else '',
                                       'fenceMode': network['Configuration']['FenceMode'],
                                       'isInherited': network['Configuration']['IpScopes']['IpScope']['IsInherited'],
                                       'gateway': network['Configuration']['IpScopes']['IpScope']['Gateway'],
                                       'netmask': network['Configuration']['IpScopes']['IpScope']['Netmask'],
                                       'subnet': network['Configuration']['IpScopes']['IpScope'][
                                           'SubnetPrefixLength'] if
                                       network['Configuration']['IpScopes']['IpScope'].get('SubnetPrefixLength') else 1,
                                       'dns1': network['Configuration']['IpScopes']['IpScope']['Dns1'] if
                                       network['Configuration']['IpScopes']['IpScope'].get('Dns1') else '',
                                       'isDeployed': network['IsDeployed']}
                        payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.MOVE_VAPP_NO_NETWORK_CONFIG_TEMPLATE)
                    networkPayloadData += payloadData.strip("\"")
        # create vApp children vm's payload
        vmPayloadData = self.createMoveVappVmPayload(vApp, targetOrgVDCId, rollback=rollback)
        if vmPayloadData and networkPayloadData:
            payloadDict = {'vAppHref': vApp['@href'],
                           'networkConfig': networkPayloadData,
                           'vmDetails': vmPayloadData}
            # creating payload data
            payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.MOVE_VAPP_TEMPLATE)
        elif vmPayloadData and not networkPayloadData:
            payloadDict = {'vAppHref': vApp['@href'],
                           'vmDetails': vmPayloadData}
            # creating payload data
            payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.MOVE_VAPP_NO_NETWORK_VM_TEMPLATE)
        payloadData = json.loads(payloadData)
        # url to compose vapp in target org vdc
        url = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                            vcdConstants.MOVE_VAPP_IN_ORG_VDC.format(targetOrgVDCId))
        self.headers["Content-Type"] = vcdConstants.XML_MOVE_VAPP
        # post api call to compose vapps in target org vdc
        response = self.restClientObj.post(url, self.headers, data=payloadData)
        if response.status_code == requests.codes.accepted:
            responseDict = xmltodict.parse(response.content)
            task = responseDict["Task"]
            taskUrl = task["@href"]
            if taskUrl:
                # checking for the status of the composing vapp task
                self._checkTaskStatus(taskUrl, timeoutForTask=timeout)
        else:
            responseDict = xmltodict.parse(response.content)
            raise Exception(
                'Failed to move vApp - {} with errors {}'.format(vApp['@name'], responseDict['Error']['@message']))
        if rollback:
            logger.info(
                'Moved vApp - {} successfully to source Org VDC'.format(vApp['@name']))
        else:
            logger.info(
                'Moved vApp - {} successfully to target Org VDC - {}'.format(vApp['@name'],
                                                                             self.sourceOrgVDCName + '-t'))
        self.rollback.key = 'moveVapp'

    @isSessionExpired
    def moveVapp(self, sourceOrgVDCId, targetOrgVDCId, targetOrgVDCNetworkList, timeout, rollback=False):
        """
        Description : Move vApp from source Org VDC to Target Org vdc
        Parameters  : sourceOrgVDCId    -   Id of the source organization VDC (STRING)
                      targetOrgVDCId    -   Id of the target organization VDC (STRING)
                      targetOrgVDCNetworkList - List of target Org VDC networks (LIST)
                      timeout  -  timeout to be used for vapp migration task (INT)
                      rollback - whether to rollback from T2V (BOOLEAN)
        """
        try:
            if rollback:
                logger.info("RollBack: Migrating Target vApps")
            # Saving rollback key
            self.rollback.key = 'moveVapp'
            sourceOrgVDCId = sourceOrgVDCId.split(':')[-1]
            # retrieving target org vdc id
            targetOrgVDCId = targetOrgVDCId.split(':')[-1]
            vAppList = self.getOrgVDCvAppsList(sourceOrgVDCId)
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
            # iterating over the source vapps
            for vApp in vAppList:
                # Spawning threads for move vApp call
                self.thread.spawnThread(self.moveVappApiCall, vApp, targetOrgVDCNetworkList, targetOrgVDCId, filePath,
                                        timeout, rollback, block=True)
                # Blocking the main thread until all the threads complete execution
            self.thread.joinThreads()
            # Checking if any thread's execution failed
            if self.thread.stop():
                raise Exception('Failed to move vApp/s')
        except Exception:
            raise
        else:
            self.rollback.executionResult['moveVapp'] = True
        finally:
            # Saving rollback key in metadata
            self.createMetaDataInOrgVDC(sourceOrgVDCId,
                                        metadataDict={'rollbackKey': self.rollback.key}, domain='system')

    @isSessionExpired
    def renameTargetNetworks(self, targetVDCId):
        """
        Description :   Renames all the target org vdc networks in the specified target Org VDC as those in source Org VDC
        Parameters  :   targetVDCId -   id of the target org vdc (STRING)
        """
        try:
            # splitting thr target org vdc id as per the xml api requirements
            targetVDCId = targetVDCId.split(':')[-1]
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE}
            # url to get the target org vdc details
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_BY_ID.format(targetVDCId))
            # get api call to retrieve the target org vdc details
            response = self.restClientObj.get(url, headers=headers)
            getResponseDict = response.json()

            # Case 1: Handling the case of renaming target org vdc networks
            # getting the list instance of all the target org vdc networks
            targetOrgVDCNetworks = getResponseDict['availableNetworks']['network'] if isinstance(getResponseDict['availableNetworks']['network'], list) else [getResponseDict['availableNetworks']['network']]
            # iterating over the target org vdc networks
            for network in targetOrgVDCNetworks:
                self.renameTargetOrgVDCNetworks(network)

            # Case 2: Handling the case renaming target vapp isolated networks
            # to get the target vapp networks, getting the target vapps
            if getResponseDict.get('resourceEntities'):
                targetOrgVDCEntityList = getResponseDict['resourceEntities']['resourceEntity'] if isinstance(getResponseDict['resourceEntities']['resourceEntity'], list) else [getResponseDict['resourceEntities']['resourceEntity']]
                vAppList = [vAppEntity for vAppEntity in targetOrgVDCEntityList if vAppEntity['type'] == vcdConstants.TYPE_VAPP]
                if vAppList:
                    self.renameTargetVappIsolatedNetworks(vAppList)
        except Exception:
            raise

    @isSessionExpired
    def getPromiscModeForgedTransmit(self, sourceOrgVDCId):
        """
        Description : Get the Promiscous Mode and Forged transmit information of source org vdc network
        """
        try:
            orgVDCNetworkList = self.getOrgVDCNetworks(sourceOrgVDCId, 'sourceOrgVDCNetworks', saveResponse=False)
            data = self.rollback.apiData
            # list of the org vdc networks with its promiscuous mode and forged transmit details
            promiscForgedList = []
            # iterating over the org vdc network list
            for orgVdcNetwork in orgVDCNetworkList:
                # url to get the dvportgroup details of org vdc network
                url = "{}{}/{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                          vcdConstants.ALL_ORG_VDC_NETWORKS, orgVdcNetwork['id'], vcdConstants.ORG_VDC_NETWORK_PORTGROUP_PROPERTIES_URI)
                # get api call to retrieve the dvportgroup details of org vdc network
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    # creating the dictionary of details of the promiscuous mode and forge transmit details
                    detailsDict = {}
                    detailsDict["id"] = orgVdcNetwork['id']
                    detailsDict["name"] = orgVdcNetwork['name']
                    detailsDict["promiscForge"] = responseDict
                    # appending the dictionary to the above list
                    promiscForgedList.append(detailsDict)
                else:
                    raise Exception('Failed to get dvportgroup properties of source Org VDC network {}'.format(orgVdcNetwork['name']))
            # writing promiscForgedList to the apiOutput.json for further use(for disabling the promiscuous mode and forged transmit in case of rollback)
            data["orgVDCNetworkPromiscModeList"] = promiscForgedList
        except Exception:
            raise

    @isSessionExpired
    def resetTargetExternalNetwork(self, networkName):
        """
        Description :   Resets the target external network(i.e updating the target external network to its initial state)
        Parameters  :   uplinkName  -   name of the source external network
        """
        try:
            logger.info('Rollback: Reset the target external network')
            data = self.rollback.apiData

            targetExternalNetworkData = self.getExternalNetwork(networkName)

            # reading the data of target external network from apiOutput.json to create payload
            payloadDict = copy.deepcopy(targetExternalNetworkData)

            # retrieving the source edge gateway uplinks before migration tool run
            edgeGatewayLinks = []
            for edgeGateway in data['sourceEdgeGateway']:
                edgeGatewayLinks += edgeGateway['edgeGatewayUplinks'] \
                    if isinstance(edgeGateway['edgeGatewayUplinks'], list) \
                    else [edgeGateway['edgeGatewayUplinks']]

            for sourceExtNet in data['sourceExternalNetwork']:
                indexToUpdate = None
                uplinkName = sourceExtNet['name']
                for sourceExtNetSubnet in sourceExtNet['subnets']['values']:
                    sourceExtNetGateway = sourceExtNetSubnet['gateway']
                    sourceExtNetPrefix = sourceExtNetSubnet['prefixLength']
                    sourceExtNetNetworkAddress = ipaddress.ip_network(
                        '{}/{}'.format(sourceExtNetGateway, sourceExtNetPrefix), strict=False)
                    for index, value in enumerate(targetExternalNetworkData['subnets']['values']):
                        targetExtNetNetworkAddress = ipaddress.ip_network(
                            '{}/{}'.format(value['gateway'], value['prefixLength']), strict=False)
                        if sourceExtNetNetworkAddress == targetExtNetNetworkAddress:
                            indexToUpdate = index
                            break
                    else:
                        continue
                    # getting the target external network's subnet ip range from apiOutput.json
                    targetExternalRange = targetExternalNetworkData['subnets']['values'][indexToUpdate]['ipRanges'][
                        'values']

                    targetExternalRangeList = []
                    # creating range of source external network pool range
                    for externalRange in targetExternalRange:
                        # breaking the iprange into list of ips covering all the ip address lying in the range
                        targetExternalRangeList.extend(
                            self.createIpRange(externalRange['startAddress'], externalRange['endAddress']))

                    sourceEdgeGatewaySubIpPools = []

                    # iterating over the edge gateway links to find the matching uplink with source external network
                    for edgeGatewayLink in edgeGatewayLinks:
                        if edgeGatewayLink['uplinkName'] == uplinkName:
                            for edgeGatewayLinkSubnet in edgeGatewayLink['subnets']['values']:
                                # Getting value of primary ip
                                primaryIp = edgeGatewayLinkSubnet.get('primaryIp')
                                # Creating ip range for primary ip
                                subIpRange = [{'startAddress': primaryIp, 'endAddress': primaryIp}]
                                # adding primary ip to sub alloacated ip pool
                                if primaryIp and ipaddress.ip_address(primaryIp) in sourceExtNetNetworkAddress:
                                    edgeGatewayLinkSubnet['ipRanges']['values'].extend(subIpRange)
                                # getting the source edge gateway's static subnet ip pool
                                if ipaddress.ip_network('{}/{}'.format(edgeGatewayLinkSubnet['gateway'],
                                                                       edgeGatewayLinkSubnet['prefixLength']),
                                                        strict=False) == sourceExtNetNetworkAddress:
                                    sourceEdgeGatewaySubIpPools += edgeGatewayLinkSubnet['ipRanges']['values']

                    sourceEdgeGatewaySubIpRangeList = []
                    for ipRange in sourceEdgeGatewaySubIpPools:
                        # breaking the iprange into list of ips covering all the ip address lying in the range
                        sourceEdgeGatewaySubIpRangeList.extend(
                            self.createIpRange(ipRange['startAddress'], ipRange['endAddress']))

                    # removing the source edge gateway's static ips from target external ip list
                    for subIp in sourceEdgeGatewaySubIpRangeList:
                        if subIp in targetExternalRangeList:
                            targetExternalRangeList.remove(subIp)

                    # creating the range of each single ip in target external network's ips
                    targetExternalNetworkStaticIpPoolList = self.createExternalNetworkSubPoolRangePayload(
                        targetExternalRangeList)

                    # assigning the targetExternalNetworkStaticIpPoolList to the ipRanges of target external network to reset it to its initial state
                    payloadDict['subnets']['values'][indexToUpdate]['ipRanges'][
                        'values'] = targetExternalNetworkStaticIpPoolList

            payloadData = json.dumps(payloadDict)
            # url to update the target external networks
            url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                   vcdConstants.ALL_EXTERNAL_NETWORKS,
                                   targetExternalNetworkData['id'])

            # setting the content type to json
            self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
            # put api call to update the target external networks
            apiResponse = self.restClientObj.put(url, self.headers, data=payloadData)
            if apiResponse.status_code == requests.codes.accepted:
                taskUrl = apiResponse.headers['Location']
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug("Successfully reset the target external network '{}' to its initial state".format(
                    targetExternalNetworkData['name']))
            else:
                errorDict = apiResponse.json()
                raise Exception("Failed to reset the target external network '{}' to its initial state: {}".format(
                    targetExternalNetworkData['name'],
                    errorDict['message']))
        except Exception:
            raise

    @isSessionExpired
    def getCatalogDetails(self, catalogHref):
        """
        Description :   Returns the details of the catalog
        Parameters: catalogHref - href of catalog for which details required (STRING)
        """
        try:
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE}
            catalogResponse = self.restClientObj.get(catalogHref, headers)
            if catalogResponse.status_code == requests.codes.ok:
                catalogResponseDict = catalogResponse.json()
                return catalogResponseDict
            else:
                errorDict = catalogResponse.json()
                raise Exception("Failed to retrieve the catalog details: {}".format(errorDict['message']))
        except Exception:
            raise

    @isSessionExpired
    def createCatalog(self, catalog, orgId):
        """
        Description :   Creates an empty placeholder catalog
        Parameters: catalog - payload dict for creating catalog (DICT)
                    orgId - Organization Id where catalog is to be created (STRING)
        """
        try:
            # create catalog url
            catalogUrl = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                       vcdConstants.CREATE_CATALOG.format(orgId))
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
            # creating the payload data
            payloadData = self.vcdUtils.createPayload(filePath,
                                                      catalog,
                                                      fileType='yaml',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.CREATE_CATALOG_TEMPLATE)
            payloadData = json.loads(payloadData)
            # setting the content-type to create a catalog
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.VCD_API_HEADER,
                       'Content-Type': vcdConstants.XML_CREATE_CATALOG}
            # post api call to create target catalogs
            createCatalogResponse = self.restClientObj.post(catalogUrl, headers, data=payloadData)
            if createCatalogResponse.status_code == requests.codes.created:
                logger.debug("Catalog '{}' created successfully".format(catalog['catalogName']))
                createCatalogResponseDict = xmltodict.parse(createCatalogResponse.content)
                # getting the newly created target catalog id
                catalogId = createCatalogResponseDict["AdminCatalog"]["@id"].split(':')[-1]
                return catalogId
            else:
                errorDict = xmltodict.parse(createCatalogResponse.content)
                raise Exception("Failed to create Catalog '{}' : {}".format(catalog['catalogName'],
                                                                            errorDict['Error']['@message']))

        except Exception:
            raise

    @isSessionExpired
    def moveCatalogItem(self, catalogItem, catalogId):
        """
        Description :   Moves the catalog Item
        Parameters : catalogItem - catalog item payload (DICT)
                     catalogId - catalog Id where this catalogitem to be moved (STRING)
        """
        try:
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
            # move catalog item url
            moveCatalogItemUrl = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                               vcdConstants.MOVE_CATALOG.format(catalogId))
            # creating the payload data to move the catalog item
            payloadData = self.vcdUtils.createPayload(filePath,
                                                      catalogItem,
                                                      fileType='yaml',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.MOVE_CATALOG_TEMPLATE)
            payloadData = json.loads(payloadData)
            # post api call to move catalog items
            response = self.restClientObj.post(moveCatalogItemUrl, self.headers, data=payloadData)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.accepted:
                task = responseDict["Task"]
                taskUrl = task["@href"]
                if taskUrl:
                    # checking the status of moving catalog item task
                    self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug("Catalog Item '{}' moved successfully".format(catalogItem['catalogItemName']))
            else:
                raise Exception('Failed to move catalog item - {}'.format(responseDict['Error']['@message']))

        except Exception:
            raise

    @description("creation of target Org VDC")
    @remediate
    def createOrgVDC(self):
        """
        Description :   Creates an Organization VDC
        """
        try:
            logger.info('Preparing Target VDC.')
            logger.info('Creating target Org VDC')
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
            data = self.rollback.apiData
            targetOrgVDCId = ''
            # organization id
            orgCompleteId = data['Organization']['@id']
            orgId = orgCompleteId.split(':')[-1]
            # retrieving organization url
            orgUrl = data['Organization']['@href']
            # retrieving source org vdc and target provider vdc data
            sourceOrgVDCPayloadDict = data["sourceOrgVDC"]
            targetPVDCPayloadDict = data['targetProviderVDC']
            targetPVDCPayloadList = [
                targetPVDCPayloadDict['StorageProfiles']['ProviderVdcStorageProfile']] if isinstance(
                targetPVDCPayloadDict['StorageProfiles']['ProviderVdcStorageProfile'], dict) else \
                targetPVDCPayloadDict['StorageProfiles']['ProviderVdcStorageProfile']
            sourceOrgVDCPayloadList = [
                sourceOrgVDCPayloadDict['VdcStorageProfiles']['VdcStorageProfile']] if isinstance(
                sourceOrgVDCPayloadDict['VdcStorageProfiles']['VdcStorageProfile'], dict) else \
                sourceOrgVDCPayloadDict['VdcStorageProfiles']['VdcStorageProfile']

            vdcStorageProfilePayloadData = ''
            # iterating over the source org vdc storage profiles
            for eachStorageProfile in sourceOrgVDCPayloadList:
                orgVDCStorageProfileDetails = self.getOrgVDCStorageProfileDetails(eachStorageProfile['@id'])
                vdcStorageProfileDict = {'vspEnabled': "true" if orgVDCStorageProfileDetails['AdminVdcStorageProfile'][
                                                                     'Enabled'] == "true" else "false",
                                         'vspUnits': 'MB',
                                         'vspLimit': str(
                                             orgVDCStorageProfileDetails['AdminVdcStorageProfile']['Limit']),
                                         'vspDefault': "true" if orgVDCStorageProfileDetails['AdminVdcStorageProfile'][
                                                                     'Default'] == "true" else "false"}
                for eachSP in targetPVDCPayloadList:
                    if eachStorageProfile['@name'] == eachSP['@name']:
                        vdcStorageProfileDict['vspHref'] = eachSP['@href']
                        vdcStorageProfileDict['vspName'] = eachSP['@name']
                        break
                eachStorageProfilePayloadData = self.vcdUtils.createPayload(filePath,
                                                                            vdcStorageProfileDict,
                                                                            fileType='yaml',
                                                                            componentName=vcdConstants.COMPONENT_NAME,
                                                                            templateName=vcdConstants.STORAGE_PROFILE_TEMPLATE_NAME)
                vdcStorageProfilePayloadData += eachStorageProfilePayloadData.strip("\"")
            # creating the payload dict
            orgVdcPayloadDict = {'orgVDCName': data["sourceOrgVDC"]["@name"] + '-t',
                                 'vdcDescription': data['sourceOrgVDC']['Description'] if data['sourceOrgVDC'].get(
                                     'Description') else '',
                                 'allocationModel': data['sourceOrgVDC']['AllocationModel'],
                                 'cpuUnits': data['sourceOrgVDC']['ComputeCapacity']['Cpu']['Units'],
                                 'cpuAllocated': data['sourceOrgVDC']['ComputeCapacity']['Cpu']['Allocated'],
                                 'cpuLimit': data['sourceOrgVDC']['ComputeCapacity']['Cpu']['Limit'],
                                 'cpuReserved': data['sourceOrgVDC']['ComputeCapacity']['Cpu']['Reserved'],
                                 'cpuUsed': data['sourceOrgVDC']['ComputeCapacity']['Cpu']['Used'],
                                 'memoryUnits': data['sourceOrgVDC']['ComputeCapacity']['Memory']['Units'],
                                 'memoryAllocated': data['sourceOrgVDC']['ComputeCapacity']['Memory']['Allocated'],
                                 'memoryLimit': data['sourceOrgVDC']['ComputeCapacity']['Memory']['Limit'],
                                 'memoryReserved': data['sourceOrgVDC']['ComputeCapacity']['Memory']['Reserved'],
                                 'memoryUsed': data['sourceOrgVDC']['ComputeCapacity']['Memory']['Used'],
                                 'nicQuota': data['sourceOrgVDC']['NicQuota'],
                                 'networkQuota': data['sourceOrgVDC']['NetworkQuota'],
                                 'vmQuota': data['sourceOrgVDC']['VmQuota'],
                                 'isEnabled': "true",
                                 'vdcStorageProfile': vdcStorageProfilePayloadData,
                                 'resourceGuaranteedMemory': data['sourceOrgVDC']['ResourceGuaranteedMemory'],
                                 'resourceGuaranteedCpu': data['sourceOrgVDC']['ResourceGuaranteedCpu'],
                                 'vCpuInMhz': data['sourceOrgVDC']['VCpuInMhz'],
                                 'isThinProvision': data['sourceOrgVDC']['IsThinProvision'],
                                 'networkPoolHref':
                                     targetPVDCPayloadDict['NetworkPoolReferences']['NetworkPoolReference']['@href'],
                                 'networkPoolId':
                                     targetPVDCPayloadDict['NetworkPoolReferences']['NetworkPoolReference']['@id'],
                                 'networkPoolName':
                                     targetPVDCPayloadDict['NetworkPoolReferences']['NetworkPoolReference']['@name'],
                                 'networkPoolType':
                                     targetPVDCPayloadDict['NetworkPoolReferences']['NetworkPoolReference']['@type'],
                                 'providerVdcHref': targetPVDCPayloadDict['@href'],
                                 'providerVdcId': targetPVDCPayloadDict['@id'],
                                 'providerVdcName': targetPVDCPayloadDict['@name'],
                                 'providerVdcType': targetPVDCPayloadDict['@type'],
                                 'usesFastProvisioning': data['sourceOrgVDC']['UsesFastProvisioning'],
                                 'defaultComputePolicy': '',
                                 'isElastic': data['sourceOrgVDC']['IsElastic'],
                                 'includeMemoryOverhead': data['sourceOrgVDC']['IncludeMemoryOverhead']}

            # retrieving org vdc compute policies
            allOrgVDCComputePolicesList = self.getOrgVDCComputePolicies()
            isSizingPolicy = False
            # getting the vm sizing policy of source org vdc
            sourceSizingPoliciesList = self.getVmSizingPoliciesOfOrgVDC(data['sourceOrgVDC']['@id'])
            if isinstance(sourceSizingPoliciesList, dict):
                sourceSizingPoliciesList = [sourceSizingPoliciesList]
            # iterating over the source org vdc vm sizing policies and check the default compute policy is sizing policy
            for eachPolicy in sourceSizingPoliciesList:
                if eachPolicy['id'] == data['sourceOrgVDC']['DefaultComputePolicy']['@id'] and eachPolicy[
                    'name'] != 'System Default':
                    # set sizing policy to true if default compute policy is sizing
                    isSizingPolicy = True
            if data['sourceOrgVDC']['DefaultComputePolicy']['@name'] != 'System Default' and not isSizingPolicy:
                # Getting the href of the compute policy if not 'System Default' as default compute policy
                orgVDCComputePolicesList = [allOrgVDCComputePolicesList] if isinstance(allOrgVDCComputePolicesList,
                                                                                       dict) else allOrgVDCComputePolicesList
                # iterating over the org vdc compute policies
                for eachComputPolicy in orgVDCComputePolicesList:
                    if eachComputPolicy["name"] == data['sourceOrgVDC']['DefaultComputePolicy']['@name'] and \
                            eachComputPolicy["pvdcId"] == data['targetProviderVDC']['@id']:
                        href = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                vcdConstants.VDC_COMPUTE_POLICIES,
                                                eachComputPolicy["id"])
                        computePolicyDict = {'defaultComputePolicyHref': href,
                                             'defaultComputePolicyId': eachComputPolicy["id"],
                                             'defaultComputePolicyName': data['sourceOrgVDC']['DefaultComputePolicy'][
                                                 '@name']}
                        computePolicyPayloadData = self.vcdUtils.createPayload(filePath,
                                                                               computePolicyDict,
                                                                               fileType='yaml',
                                                                               componentName=vcdConstants.COMPONENT_NAME,
                                                                               templateName=vcdConstants.COMPUTE_POLICY_TEMPLATE_NAME)
                        orgVdcPayloadDict['defaultComputePolicy'] = computePolicyPayloadData.strip("\"")
                        break
                else:  # for else (loop else)
                    raise Exception(
                        "No Target Compute Policy found with same name as Source Org VDC default Compute Policy and belonging to the target Provider VDC.")
            # if sizing policy is set, default compute policy is vm sizing polciy
            if isSizingPolicy:
                computePolicyDict = {'defaultComputePolicyHref': data['sourceOrgVDC']['DefaultComputePolicy']['@href'],
                                     'defaultComputePolicyId': data['sourceOrgVDC']['DefaultComputePolicy']['@id'],
                                     'defaultComputePolicyName': data['sourceOrgVDC']['DefaultComputePolicy']['@name']}
                computePolicyPayloadData = self.vcdUtils.createPayload(filePath,
                                                                       computePolicyDict,
                                                                       fileType='yaml',
                                                                       componentName=vcdConstants.COMPONENT_NAME,
                                                                       templateName=vcdConstants.COMPUTE_POLICY_TEMPLATE_NAME)
                orgVdcPayloadDict['defaultComputePolicy'] = computePolicyPayloadData.strip("\"")
            orgVdcPayloadData = self.vcdUtils.createPayload(filePath,
                                                            orgVdcPayloadDict,
                                                            fileType='yaml',
                                                            componentName=vcdConstants.COMPONENT_NAME,
                                                            templateName=vcdConstants.CREATE_ORG_VDC_TEMPLATE_NAME)

            payloadData = json.loads(orgVdcPayloadData)

            # url to create org vdc
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.CREATE_ORG_VDC.format(orgId))
            self.headers["Content-Type"] = vcdConstants.XML_CREATE_VDC_CONTENT_TYPE
            # post api to create org vdc

            response = self.restClientObj.post(url, self.headers, data=payloadData)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.created:
                taskId = responseDict["AdminVdc"]["Tasks"]["Task"]
                if isinstance(taskId, dict):
                    taskId = [taskId]
                for task in taskId:
                    if task["@operationName"] == vcdConstants.CREATE_VDC_TASK_NAME:
                        taskUrl = task["@href"]
                        # Fetching target org vdc id for deleting target vdc in case of failure
                        targetOrgVDCId = re.search(r'\((.*)\)', task['@operation']).group(1)
                if taskUrl:
                    # checking the status of the task of creating the org vdc
                    self._checkTaskStatus(taskUrl=taskUrl)
                logger.info('Target Org VDC {} created successfully'.format(data["sourceOrgVDC"]["@name"] + '-t'))
                # returning the id of the created org vdc
                return self.getOrgVDCDetails(orgUrl, responseDict['AdminVdc']['@name'], 'targetOrgVDC')
            raise Exception('Failed to create target Org VDC. Errors {}.'.format(responseDict['Error']['@message']))
        except Exception as exception:
            if targetOrgVDCId:
                logger.debug("Creation of target vdc failed, so removing that entity from vCD")
                try:
                    self.deleteOrgVDC(targetOrgVDCId)
                except Exception as e:
                    errorMessage = f'No access to entity "com.vmware.vcloud.entity.vdc:{targetOrgVDCId}'
                    if errorMessage in str(e):
                        pass
                    else:
                        raise Exception('Failed to delete target org vdc during rollback')
            raise exception

    @description(desc="enabling the promiscuous mode and forged transmit on source Org VDC networks")
    @remediate
    def enablePromiscModeForgedTransmit(self, orgVDCNetworkList):
        """
        Description : Enabling Promiscuous Mode and Forged transmit of source org vdc network
        Parameters  : orgVDCNetworkList - List containing source org vdc networks (LIST)
        """
        try:
            logger.info('Enabling the promiscuous mode and forged transmit on source Org VDC networks.')
            # if call to disable to promiscuous mode then orgVDCNetworkList will be retrieved from apiOutput.json
            # iterating over the orgVDCNetworkList
            for orgVdcNetwork in orgVDCNetworkList:
                # url to get the dvportgroup details of org vdc network
                url = "{}{}/{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                          vcdConstants.ALL_ORG_VDC_NETWORKS,
                                          orgVdcNetwork['id'],
                                          vcdConstants.ORG_VDC_NETWORK_PORTGROUP_PROPERTIES_URI)
                # get api call to retrieve the dvportgroup details of org vdc network
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    # if enable call then setting the mode True
                    responseDict['dvpgProperties'][0]['promiscuousMode'] = True
                    responseDict['dvpgProperties'][0]['forgedTransmit'] = True
                    payloadData = json.dumps(responseDict)
                    payloadData = json.loads(payloadData)
                    payloadData = json.dumps(payloadData)
                    # updating the org vdc network dvportgroup properties
                    self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                    # put api call to update the promiscuous mode and forged mode
                    apiResponse = self.restClientObj.put(url, self.headers, data=payloadData)
                    if apiResponse.status_code == requests.codes.accepted:
                        taskUrl = apiResponse.headers['Location']
                        # checking the status of the updating dvpgportgroup properties of org vdc network task
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug('Successfully enabled source Org VDC Network {} dvportgroup properties.'.format(
                            orgVdcNetwork['name']))
                    else:
                        errorResponse = apiResponse.json()
                        raise Exception(
                            'Failed to enable dvportgroup properties of source Org VDC network {} - {}'.format(
                                orgVdcNetwork['name'], errorResponse['message']))
                else:
                    raise Exception('Failed to get dvportgroup properties of source Org VDC network {}'.format(
                        orgVdcNetwork['name']))
        except Exception:
            raise

    @isSessionExpired
    def disablePromiscModeForgedTransmit(self):
        """
        Description : Disabling Promiscuous Mode and Forged transmit of source org vdc network
        """
        try:
            logger.info("RollBack: Restoring the Promiscuous Mode and Forged Mode")
            data = self.rollback.apiData
            orgVDCNetworkList = data["orgVDCNetworkPromiscModeList"]
            # iterating over the orgVDCNetworkList
            for orgVdcNetwork in orgVDCNetworkList:
                # url to get the dvportgroup details of org vdc network
                url = "{}{}/{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                          vcdConstants.ALL_ORG_VDC_NETWORKS,
                                          orgVdcNetwork['id'],
                                          vcdConstants.ORG_VDC_NETWORK_PORTGROUP_PROPERTIES_URI)
                # get api call to retrieve the dvportgroup details of org vdc network
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    # disable call then setting the mode to its initial state by retrieving from apiOutput.json
                    if not orgVdcNetwork['promiscForge']['dvpgProperties'][0]['promiscuousMode']:
                        responseDict['dvpgProperties'][0][
                            'promiscuousMode'] = False  # orgVdcNetwork['promiscForge']['dvpgProperties'][0]['promiscuousMode']
                    if not orgVdcNetwork['promiscForge']['dvpgProperties'][0]['forgedTransmit']:
                        responseDict['dvpgProperties'][0][
                            'forgedTransmit'] = False  # orgVdcNetwork['promiscForge']['dvpgProperties'][0]['forgedTransmit']
                    payloadData = json.dumps(responseDict)
                    payloadData = json.loads(payloadData)
                    payloadData = json.dumps(payloadData)
                    # updating the org vdc network dvportgroup properties
                    self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                    # put api call to update the promiscuous mode and forged mode
                    apiResponse = self.restClientObj.put(url, self.headers, data=payloadData)
                    if apiResponse.status_code == requests.codes.accepted:
                        taskUrl = apiResponse.headers['Location']
                        # checking the status of the updating dvpgportgroup properties of org vdc network task
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug('Successfully disabled source Org VDC Network {} dvportgroup properties.'.format(
                            orgVdcNetwork['name']))
                    else:
                        errorResponse = apiResponse.json()
                        raise Exception(
                            'Failed to disabled dvportgroup properties of source Org VDC network {} - {}'.format(
                                orgVdcNetwork['name'], errorResponse['message']))
                else:
                    raise Exception('Failed to get dvportgroup properties of source Org VDC network {}'.format(
                        orgVdcNetwork['name']))
        except Exception:
            raise

    @isSessionExpired
    def renameVappNetworks(self, vAppNetworkHref):
        """
        Description :   Renames the vApp isolated network back to its original name
                        (i.e removes the trailing -v2t string of the vapp network name)
        Parameters  :   vAppNetworkHref -   href of the vapp network (STRING)
        """
        try:
            # setting the headers required for the api
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE}
            # get api call to retrieve the vapp isolated networks' details
            vAppNetworkResponse = self.restClientObj.get(vAppNetworkHref, headers)
            vAppNetworkResponseDict = vAppNetworkResponse.json()
            # changing the name of the vapp isolated network
            vAppNetworkResponseDict['name'] = vAppNetworkResponseDict['name'][
                                              0: len(vAppNetworkResponseDict['name']) - 4]
            # creating the payload data
            payloadData = json.dumps(vAppNetworkResponseDict)
            # setting the content-type required for the api
            headers['Content-Type'] = vcdConstants.VAPP_NETWORK_CONTENT_TYPE
            # put api call to update rename the target vapp isolated network
            putResponse = self.restClientObj.put(vAppNetworkHref, headers=headers, data=payloadData)
            if putResponse.status_code == requests.codes.ok:
                logger.debug(
                    "Target vApp Isolated Network successfully renamed to '{}'".format(vAppNetworkResponseDict['name']))
            else:
                putResponseDict = putResponse.json()
                raise Exception("Failed to rename the target vApp Isolated Network '{}' : {}".format(
                    vAppNetworkResponseDict['name'] + '-v2t',
                    putResponseDict['message']))
            # sleep for 5 seconds before deleting next network
            time.sleep(5)
        except Exception:
            raise

    @isSessionExpired
    def renameTargetVappIsolatedNetworks(self, vAppList):
        """
        Description :   Renames all the vApp isolated networks for each vApp in the specified vApps list
        Parameters  :   vAppList    -   list of details of target vApps (LIST)
        """
        try:
            # iterating over the target vapps
            for vApp in vAppList:
                # get api call to retrieve the details of target vapp
                vAppResponse = self.restClientObj.get(vApp['href'], self.headers)
                vAppResponseDict = xmltodict.parse(vAppResponse.content)
                vAppData = vAppResponseDict['VApp']
                # checking for the networks in the vapp
                if vAppData['NetworkConfigSection'].get('NetworkConfig'):
                    vAppNetworkList = vAppData['NetworkConfigSection']['NetworkConfig'] if isinstance(vAppData['NetworkConfigSection']['NetworkConfig'], list) else [vAppData['NetworkConfigSection']['NetworkConfig']]
                    if vAppNetworkList:
                        # iterating over the networks in vapp
                        for vAppNetwork in vAppNetworkList:
                            # handling only vapp isolated networks whose name ends with -v2t
                            if vAppNetwork['Configuration']['FenceMode'] == "isolated" and vAppNetwork['@networkName'].endswith('-v2t'):
                                vAppLinksList = vAppData['Link'] if isinstance(vAppData['Link'], list) else [vAppData['Link']]
                                # iterating over the vAppLinksList to get the vapp isolated networks' href
                                for link in vAppLinksList:
                                    if link.get('@name'):
                                        if link['@name'] == vAppNetwork['@networkName'] and 'admin' not in link['@href']:
                                            vAppNetworkHref = link['@href']
                                            break
                                else:
                                    logger.debug("Failed to rename the target isolated network '{}', since failed to get href".format(vAppNetwork['@networkName']))
                                    continue
                                self.renameVappNetworks(vAppNetworkHref)
        except Exception:
            raise

    @isSessionExpired
    def renameTargetOrgVDCNetworks(self, network):
        """
        Description :   Renames the target org VDC networks back to its original name
                        (i.e removes the trailing -v2t from the target org VDC network name)
        Parameters  :   network -   details of the network that is to be renamed (DICT)
        """
        try:
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE}
            # open api get url to retrieve the details of target org vdc network
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.GET_ORG_VDC_NETWORK_BY_ID.format(network['id']))
            # get api call to retrieve the details of target org vdc network
            networkResponse = self.restClientObj.get(url, headers=self.headers)
            networkResponseDict = networkResponse.json()

            # checking if the target org vdc network name endwith '-v2t', if so removing the '-v2t' from the name
            if networkResponseDict['name'].endswith('-v2t'):
                # getting the original name of the
                networkResponseDict['name'] = networkResponseDict['name'][0: len(networkResponseDict['name']) - 4]
                # creating the payload data of the retrieved details of the org vdc network
                payloadData = json.dumps(networkResponseDict)
                # setting the content-type as per the api requirement
                headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                # put api call to rename the target org vdc network
                putResponse = self.restClientObj.put(url, headers=headers, data=payloadData)
                if putResponse.status_code == requests.codes.accepted:
                    taskUrl = putResponse.headers['Location']
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug("Target Org VDC Network '{}' renamed successfully".format(networkResponseDict['name']))
                else:
                    errorDict = putResponse.json()
                    raise Exception("Failed to rename the target org VDC to '{}' : {}".format(networkResponseDict['name'],
                                                                                              errorDict['message']))

        except Exception:
            raise

    @description('Creating Org vDC groups in Target Org VDC')
    @remediate
    def createOrgvDCGroup(self, sourceOrgVDCName):
        """
        Description: Creating Org vDC group with single Org vDC
        parameter: sourceOrgVDCName -  Name of the source orgVDC (STRING)
        """
        try:
            if not self.rollback.apiData.get('OrgVDCGroupID'):
                sourceOrgVDCId = self.rollback.apiData['sourceOrgVDC']['@id']
                allLayer3Rules = self.getDistributedFirewallConfig(sourceOrgVDCId)
                targetEdgegateways = self.rollback.apiData['targetEdgeGateway']
                conflictNetworks = self.rollback.apiData.get('ConflictNetworks')
                if not conflictNetworks:
                    conflictNetworks = []
                gatewayList = targetEdgegateways + conflictNetworks
                ownerIds = dict()
                if allLayer3Rules:
                    logger.info('Org VDC group is getting created')
                    for targetEdgegateway in gatewayList:
                        # open api to create Org vDC group
                        url = '{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.VDC_GROUPS)
                        targetOrgVDCId = self.rollback.apiData['targetOrgVDC']['@id']
                        organizationId = self.rollback.apiData['Organization']['@id']
                        payloadDict = {'orgId': organizationId, 'name': sourceOrgVDCName + '-Group-' + targetEdgegateway['name'],
                                       'participatingOrgVdcs': [{
                                           'vdcRef': {'id': targetOrgVDCId}, 'orgRef': {'id': organizationId}}],
                                       'type': 'LOCAL', 'networkProviderType': 'NSX_T'
                                       }
                        payloadData = json.dumps(payloadDict)
                        # setting the content-type as per the api requirement
                        self.headers['Content-Type'] = 'application/json'
                        response = self.restClientObj.post(url, self.headers, data=payloadData)
                        if response.status_code == requests.codes.accepted:
                            taskUrl = response.headers['Location']
                            header = {'Authorization': self.headers['Authorization'],
                                      'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE}
                            taskResponse = self.restClientObj.get(url=taskUrl, headers=header)
                            responseDict = taskResponse.json()
                            self._checkTaskStatus(taskUrl=taskUrl)
                            ownerIds.update({targetEdgegateway['id']: responseDict['owner']['id']})
                            self.rollback.apiData['OrgVDCGroupID'] = ownerIds
                            logger.debug(
                                "Target Org VDC Group '{}' created successfully".format(sourceOrgVDCName + '-Group-' + targetEdgegateway['name']))
                        else:
                            errorDict = response.json()
                            raise Exception("Failed to create target org VDC Group '{}' ".format(errorDict['message']))
        except Exception:
            raise

    @description('Enable DFW in Orgvdc group')
    @remediate
    def enableDFWinOrgvdcGroup(self, rollback=False):
        """
        Description :   Enable DFW in Orgvdc group
        Parameters  :   rollback- True to disable DFW in ORG VDC group
        """
        try:
            orgvDCgroupIds = self.rollback.apiData['OrgVDCGroupID'].values() if self.rollback.apiData.get('OrgVDCGroupID') else []
            for orgvDCgroupId in orgvDCgroupIds:
                if rollback:
                    url = '{}{}{}/default'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                          vcdConstants.GET_VDC_GROUP_BY_ID.format(orgvDCgroupId),
                                          vcdConstants.ENABLE_DFW_POLICY)
                    logger.debug('DFW is getting disabled in Org VDC group id: {}'.format(orgvDCgroupId))
                    payloadDict = {"id": "default", "name": "Default", "enabled": False}
                else:
                    url = '{}{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                          vcdConstants.GET_VDC_GROUP_BY_ID.format(orgvDCgroupId),
                                          vcdConstants.ENABLE_DFW_POLICY)
                    logger.debug('DFW is getting enabled in Org VDC group id: {}'.format(orgvDCgroupId))
                    payloadDict = {"enabled": True, "defaultPolicy": {"name": "defaultPolicy Allow", "enabled": True}}
                payloadData = json.dumps(payloadDict)
                # setting the content-type as per the api requirement
                self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                response = self.restClientObj.put(url, self.headers, data=payloadData)
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    header = {'Authorization': self.headers['Authorization'],
                              'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE}
                    taskResponse = self.restClientObj.get(url=taskUrl, headers=header)
                    responseDict = taskResponse.json()
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug("DFW is enabled successfully on VDC group id: {}".format(orgvDCgroupId))
                else:
                    errorDict = response.json()
                    raise Exception("Failed to enable DFW '{}' ".format(errorDict['message']))
            self.dfwRulesRollback(rollback=False)
        except Exception:
            raise

    @description('Increasing/Decreasing the scope of Edge gateways')
    @remediate
    def increaseScopeOfEdgegateways(self, rollback=False):
        """
        Description: Increasing the scope of Edge gateways to VDC group
        parameter: rollback- True to decrease the scope of edgegateway from NSX-T ORG VDC
        """
        try:
            edgeGatewayList = self.rollback.apiData['targetEdgeGateway']
            sourceOrgVDCId = self.rollback.apiData['sourceOrgVDC']['@id']
            allLayer3Rules = self.getDistributedFirewallConfig(sourceOrgVDCId)
            if allLayer3Rules:
                ownerRefIDs = self.rollback.apiData.get('OrgVDCGroupID')
                targetOrgVdcId = self.rollback.apiData['targetOrgVDC']['@id']
                for edgeGateway in edgeGatewayList:
                    if rollback:
                        logger.debug('Decreasing the scope of Edge gateway - {}'.format(edgeGateway['name']))
                    else:
                        logger.debug('Increasing the scope of Edge gateway - {}'.format(edgeGateway['name']))
                    # url to update external network properties
                    url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                           vcdConstants.ALL_EDGE_GATEWAYS, edgeGateway['id'])
                    header = {'Authorization': self.headers['Authorization'],
                              'Accept': vcdConstants.OPEN_API_CONTENT_TYPE+';version=35.0'}
                    response = self.restClientObj.get(url, header)
                    if response.status_code == requests.codes.ok:
                        responseDict = response.json()
                        if rollback:
                            # changing the owner reference from org VDC to org VDC group
                            responseDict['ownerRef'] = {'id': targetOrgVdcId}
                        else:
                            ownerRefID = ownerRefIDs[edgeGateway['id']] if ownerRefIDs.get(edgeGateway['id']) else ''
                            # changing the owner reference from org VDC to org VDC group
                            responseDict['ownerRef'] = {'id': ownerRefID}
                        payloadData = json.dumps(responseDict)
                        self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                        # put call to increase the scope of the edgegateway
                        response = self.restClientObj.put(url, self.headers, data=payloadData)
                        if response.status_code == requests.codes.accepted:
                            # successful creation of firewall group
                            taskUrl = response.headers['Location']
                            self._checkTaskStatus(taskUrl, returnOutput=False)
                            logger.debug('Successfully changed the scope of Edge gateway - {}'.format(edgeGateway['name']))
                        else:
                            errorResponse = response.json()
                            # failure in increase scope of the network
                            raise Exception('Failed to increase scope of the EdgeGateway {} - {}'.format(responseDict['name'], errorResponse['message']))
                    else:
                        responseDict = response.json()
                        raise Exception('Failed to retrieve Edgewateway- {}'.format(responseDict['message']))

        except Exception:
            raise

    @isSessionExpired
    def deleteOrgVDCGroup(self):
        """
        Description: Deleting the ORG VDC group as part of rollback
        """
        try:
            ownerRefIDs = self.rollback.apiData.get('OrgVDCGroupID')
            if ownerRefIDs:
                # url to get Org vDC groups
                url = '{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.VDC_GROUPS)
                self.headers['Content-Type'] = 'application/json'
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    vdcGroupsData = response.json()
                    vdcGroups = vdcGroupsData['values']
                    vdcGroupsIds = [group['id'] for group in vdcGroups]
                else:
                    errorDict = response.json()
                    raise Exception("Failed to get target org VDC Group '{}' ".format(errorDict['message']))

                for ownerRefID in ownerRefIDs.values():
                    if ownerRefID in vdcGroupsIds:
                        # open api to create Org vDC group
                        url = '{}{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.VDC_GROUPS, ownerRefID)
                        response = self.restClientObj.delete(url, self.headers)
                        if response.status_code == requests.codes.accepted:
                            taskUrl = response.headers['Location']
                            self._checkTaskStatus(taskUrl=taskUrl)
                        else:
                            response = response.json()
                            raise Exception("Failed to delete ORG VDC group from target - {}".format(response['message']))
        except Exception:
            raise
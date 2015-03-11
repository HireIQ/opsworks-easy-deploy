#!/usr/bin/python

#
# Copyright 2014 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#  http://aws.amazon.com/apache2.0
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.
#

import click
import os
import sys
import time
import json
import arrow
import botocore.session


class Operation(object):
    def __init__(self, context):
        self.session = botocore.session.get_session()
        self.service_endpoints = {
            'opsworks': context.obj['OPSWORKS_REGION'],
            'elb': context.obj['ELB_REGION']
        }
        self.stack_name = None
        self.layer_name = None
        self.deploy_timeout = None
        self._stack_id = None
        self._layer_id = None

        self.pre_deployment_hooks = []
        self.post_deployment_hooks = []

    def init(self, stack_name, timeout=None, layer_name=None):
        self.stack_name = stack_name
        self.layer_name = layer_name
        self.deploy_timeout = timeout

    @property
    def stack_id(self):
        if self._stack_id is None:
            stacks = self._make_api_call('opsworks', 'DescribeStacks')['Stacks']
            for stack in stacks:
                stack_id = stack['StackId']
                if self.stack_name == stack['Name'].lower():
                    self._stack_id = stack_id
                    break
            else:
                log("Stack {0} not found.  Aborting".format(self.stack_name))
                sys.exit(1)
        return self._stack_id

    @property
    def layer_id(self):
        if self._layer_id is None:
            layers = self._make_api_call('opsworks', 'DescribeLayers', stack_id=self.stack_id)['Layers']
            for each_layer in layers:
                layer_id = each_layer['LayerId']
                if self.layer_name == each_layer['Name'].lower():
                    self._layer_id = layer_id
                    break
            else:
                log("No Layer found with name {0} in stack {1}.  Aborting".format(self.layer_name, self.stack_name))
                sys.exit(1)
        return self._layer_id

    def layer_at_once(self, comment, custom_json, exclude_hosts=None):
        all_instances = self._make_api_call('opsworks', 'DescribeInstances', layer_id=self.layer_id)

        if exclude_hosts is None:
            exclude_hosts = []

        deployment_instance_ids = []
        for each in all_instances['Instances']:
            if each['Status'] == 'online' and each['Hostname'] not in exclude_hosts:
                deployment_instance_ids.append(each['InstanceId'])
        self._deploy_to(instance_ids=deployment_instance_ids, name="{0} instances".format(self.layer_name), comment=comment, custom_json=custom_json)

    def layer_rolling(self, comment, custom_json):
        load_balancer_name = self._get_opsworks_elb_name()

        if load_balancer_name is not None:
            self.pre_deployment_hooks.append(self._remove_instance_from_elb)
            self.post_deployment_hooks.append(self._add_instance_to_elb)

        all_instances = self._make_api_call('opsworks', 'DescribeInstances', layer_id=self.layer_id)
        for each in all_instances['Instances']:
            if each['Status'] != 'online':
                continue

            hostname = each['Hostname']
            instance_id = each['InstanceId']
            ec2_instance_id = each['Ec2InstanceId']

            self._deploy_to(instance_ids=[instance_id], name=hostname, comment=comment, custom_json=custom_json, load_balancer_name=load_balancer_name, ec2_instance_id=ec2_instance_id)

    def instances_at_once(self, host_names, comment, custom_json):
        all_instances = self._make_api_call('opsworks', 'DescribeInstances', stack_id=self.stack_id)

        deployment_instance_ids = []
        for each in all_instances['Instances']:
            if each['Status'] == 'online' and each['Hostname'] in host_names:
                deployment_instance_ids.append(each['InstanceId'])

        self._deploy_to(instance_ids=deployment_instance_ids, name=", ".join(host_names), comment=comment, custom_json=custom_json)

    def post_elb_registration(self, hostname, load_balancer_name):
        describe_result = self._make_api_call('elb', 'DescribeLoadBalancers', load_balancer_names=[load_balancer_name])
        healthy_threshold = describe_result['LoadBalancerDescriptions'][0]['HealthCheck']['HealthyThreshold']
        interval = describe_result['LoadBalancerDescriptions'][0]['HealthCheck']['Interval']

        instance_healthy_wait = (healthy_threshold * interval)
        log("Added {0} to ELB {1}.  Sleeping for {2} seconds for it to be online".format(hostname, load_balancer_name, instance_healthy_wait))
        time.sleep(instance_healthy_wait)

    def _get_opsworks_elb_name(self):
        """
        Get an OpsWorks ELB Name of the layer id in the stack if is associated with the layer
        :return: Elastic Load Balancer name if associated with the layer, otherwise None
        """
        elbs = self._make_api_call('opsworks', 'DescribeElasticLoadBalancers', layer_ids=[self.layer_id])
        if len(elbs['ElasticLoadBalancers']) > 0:
            return elbs['ElasticLoadBalancers'][0]['ElasticLoadBalancerName']
        else:
            return None

    def _deploy_to(self, **kwargs):
        for pre_deploy in self.pre_deployment_hooks:
            pre_deploy(**kwargs)

        arguments = self._create_deployment_arguments(kwargs['instance_ids'], kwargs['comment'], kwargs['custom_json'])
        deployment = self._make_api_call('opsworks', 'CreateDeployment', **arguments)

        deployment_id = deployment['DeploymentId']
        log("Deployment {0} to {1} requested - command: {2}".format(deployment_id, kwargs['name'], self.command))

        self._poll_deployment_complete(deployment_id)

        for post_deploy in self.post_deployment_hooks:
            post_deploy(**kwargs)

    def _create_deployment_arguments(self, instance_ids, comment, custom_json):
        raise NotImplemented('Method must be implemented in child class')

    def _poll_deployment_complete(self, deployment_id):
        start_time = time.time()
        while True:
            deployment_status = self._make_api_call('opsworks', 'DescribeDeployments', deployment_ids=[deployment_id])

            for each in deployment_status['Deployments']:
                if each['DeploymentId'] == deployment_id:
                    if each['Status'] == 'successful':
                        log("Deployment {0} completed successfully at {1} after {2} seconds".format(deployment_id, each['CompletedAt'], self._get_deployment_duration(each).seconds))
                        return

                    if each['Status'] == 'failed':
                        log("Deployment {0} failed in {1} seconds".format(deployment_id, self._get_deployment_duration(each).seconds))
                        sys.exit(1)

                    log("Deployment {0} is currently {1}".format(deployment_id, each['Status']))
                    continue

            elapsed_time = time.time() - start_time
            if self.deploy_timeout is not None and elapsed_time > self.deploy_timeout:
                log("Deployment {0} has exceeded the timeout of {1} seconds.  Aborting".format(deployment_id, self.deploy_timeout))
                sys.exit(1)
            time.sleep(20)

    @staticmethod
    def _get_deployment_duration(deployment_status):
        """
        Given a deployment status, calculate and return the duration.
        For some reason the "Duration" parameter is not always populated
        from the OpsWorks API, so this works around that.
        :param deployment_status:
        :return:
        """
        started_at = arrow.get(deployment_status['CreatedAt'])
        completed_at = arrow.get(deployment_status['CompletedAt'])
        return completed_at - started_at

    def _add_instance_to_elb(self, **kwargs):
        self._make_api_call('elb', 'RegisterInstancesWithLoadBalancer',
                            load_balancer_name=kwargs['load_balancer_name'],
                            instances=[{'InstanceId': kwargs['ec2_instance_id']}])

        self.post_elb_registration(kwargs['name'], kwargs['load_balancer_name'])

        if not self._is_instance_healthy(kwargs['load_balancer_name'], kwargs['ec2_instance_id']):
            log("Instance {0} did not come online after deploy. Aborting remaining deployment".format(kwargs['name']))
            sys.exit(1)

    def _remove_instance_from_elb(self, **kwargs):
        deregister_response = self._make_api_call('elb', 'DeregisterInstancesFromLoadBalancer',
                                                  load_balancer_name=kwargs['load_balancer_name'],
                                                  instances=[{'InstanceId': kwargs['ec2_instance_id']}])
        log("Removed {0} from ELB {1}. There are still {2} instance(s) online".format(kwargs['name'], kwargs['load_balancer_name'], len(deregister_response['Instances'])))

        self._wait_for_elb(kwargs['load_balancer_name'])

    def _wait_for_elb(self, load_balancer_name):
        elb_attributes = self._make_api_call('elb', 'DescribeLoadBalancerAttributes',
                                             load_balancer_name=load_balancer_name)
        if 'ConnectionDraining' in elb_attributes['LoadBalancerAttributes']:
            connection_draining = elb_attributes['LoadBalancerAttributes']['ConnectionDraining']
            if connection_draining['Enabled']:
                log("Connection Draining enabled - sleeping for {0} seconds".format(connection_draining['Timeout']))
                time.sleep(connection_draining['Timeout'])
                return

        log("Connection Draining not enabled - sleeping for 20 seconds")
        time.sleep(20)

    def _is_instance_healthy(self, load_balancer_name, instance_id):
        instance_health = self._make_api_call('elb', 'DescribeInstanceHealth',
                                              load_balancer_name=load_balancer_name,
                                              instances=[{'InstanceId': instance_id}])

        for each in instance_health['InstanceStates']:
            if each['InstanceId'] == instance_id:
                status_detail = ""
                if each['State'] != 'InService':
                    status_detail = " ({0} - {1})".format(each['ReasonCode'], each['Description'])
                log("Current instance state is {0}{1}".format(each['State'], status_detail))
                return each['State'] == 'InService'

        return False

    def _make_api_call(self, service_name, api_operation, **kwargs):
        """
        Make an API call using botocore for the given service and api operation.
        :param service_name: AWS Service name (all lowercase)
        :param api_operation: Operation name to perform
        :param kwargs: Any additional arguments to be passed to the service call
        :return: If an OK response returned, returns the data from the call.  Will exit(1) otherwise
        """
        service = self.session.get_service(service_name)
        endpoint_region = self.service_endpoints.get(service_name, 'us-east-1')
        endpoint = service.get_endpoint(endpoint_region)
        operation = service.get_operation(api_operation)
        response, response_data = operation.call(endpoint, **kwargs)
        if response.ok:
            return response_data

        log("Error occurred calling {0} - {1} - Status {2} Message {3}".format(response.url, api_operation, response.status_code, response.text))
        sys.exit(1)


class Update(Operation):
    """
    Used to issue an Update Dependencies operation within OpsWorks
    """
    def __init__(self, context):
        self.allow_reboot = False
        self.amazon_linux_release = None
        self.reboot_delay = 300

        super(Update, self).__init__(context)
        self.post_deployment_hooks.append(self.wait_for_reboot)

    @property
    def command(self):
        return 'update_dependencies'

    def wait_for_reboot(self, **kwargs):
        """
        Additional buffer when performing updates
        """
        if self.allow_reboot:
            log("Sleeping {0} seconds to allow {1} to reboot (if required)".format(self.reboot_delay, kwargs['name']))
            time.sleep(self.reboot_delay)

    def _create_deployment_arguments(self, instance_ids, comment, custom_json):
        custom = {
            'dependencies': {
                'allow_reboot': self.allow_reboot
            }
        }
        if self.amazon_linux_release is not None:
            custom_json['dependencies']['os_release_version'] = self.amazon_linux_release

        parsed_json = parse_custom_json(custom_json)
        custom.update(parsed_json)
        return {
            'stack_id': self.stack_id,
            'instance_ids': instance_ids,
            'command': {'Name': self.command},
            'comment': comment,
            'custom_json': json.dumps(custom)
        }


class Deploy(Operation):
    """
    Used to issue a Deployment operation within OpsWorks
    """
    def __init__(self, context):
        self.application_name = None
        self._application_id = None

        super(Deploy, self).__init__(context)

    @property
    def command(self):
        return 'deploy'

    @property
    def application_id(self):
        if self._application_id is None:
            applications = self._make_api_call('opsworks', 'DescribeApps', stack_id=self.stack_id)
            for each in applications['Apps']:
                if each['Shortname'] == self.application_name:
                    self._application_id = each['AppId']
                    break

            if self._application_id is None:
                log("Application {0} not found in stack {1}.  Aborting".format(self.application_name, self.stack_name))
                sys.exit(1)

        return self._application_id

    def _create_deployment_arguments(self, instance_ids, comment, custom_json):
        parsed_json = parse_custom_json(custom_json)
        return {
            'stack_id': self.stack_id,
            'app_id': self.application_id,
            'instance_ids': instance_ids,
            'command': {'Name': self.command},
            'comment': comment,
            'custom_json': json.dumps(parsed_json)
        }


def log(message):
    click.echo("[{0}] {1}".format(arrow.utcnow().format('YYYY-MM-DD HH:mm:ss ZZ'), message))

def parse_custom_json(custom_json):
    parsed = {}
    if custom_json is not None:
        json_data = custom_json
        if custom_json.strip()[:1] != '{':
            json_data=open(custom_json).read()
        parsed = json.loads(json_data)
    return parsed

@click.group(chain=True)
@click.option('--profile', type=click.STRING, help='Profile used to lookup credentials.')
@click.option('--opsworks-region', type=click.STRING, default='us-east-1', help="OpsWorks region endpoint")
@click.option('--elb-region', type=click.STRING, default='us-east-1', help="Elastic Load Balancer region endpoint")
@click.pass_context
def cli(ctx, profile, opsworks_region, elb_region):
    if profile is not None:
        os.environ['BOTO_DEFAULT_PROFILE'] = profile
    ctx.obj['OPSWORKS_REGION'] = opsworks_region
    ctx.obj['ELB_REGION'] = elb_region


@cli.command(help='Installs regular operating system updates and package updates')
@click.option('--allow-reboot/--no-all-reboot', default=False, help='Allow OpsWorks to reboot instance if kernel was updated')
@click.option('--amazon-linux-release', type=click.STRING, help='Set the Amazon Linux version, only use it when OpsWorks has support for it')
@click.pass_context
def update(ctx, allow_reboot, amazon_linux_release):
    operation = Update(ctx)
    operation.allow_reboot = allow_reboot
    operation.amazon_linux_release = amazon_linux_release
    ctx.obj['OPERATION'] = operation


@cli.command(help='Deploys an application')
@click.option('--application', type=click.STRING, required=True, help='OpsWorks Application')
@click.pass_context
def deploy(ctx, application):
    operation = Deploy(ctx)
    operation.application_name = application
    ctx.obj['OPERATION'] = operation


@cli.command(help='Execute operation on all hosts in the layer at once')
@click.option('--stack-name', type=click.STRING, required=True, help='OpsWorks Stack name')
@click.option('--layer-name', help='Layer to deploy application to')
@click.option('--exclude-hosts', '-x', default=None, help='Host names to exclude from deployment (comma separated list)')
@click.option('--comment', help='Deployment message')
@click.option('--timeout', default=None, help='Deployment timeout')
@click.option('--custom_json', default=None, help='Custom json filepath or native json string')
@click.pass_context
def all(ctx, stack_name, layer_name, exclude_hosts, comment, timeout, custom_json):
    operation = ctx.obj['OPERATION']
    operation.init(stack_name=stack_name, layer_name=layer_name, timeout=timeout)
    if exclude_hosts is not None:
        exclude_hosts = exclude_hosts.split(',')
    operation.layer_at_once(comment=comment, custom_json=custom_json, exclude_hosts=exclude_hosts)


@cli.command(help='Rolling execution of operation to all hosts in the layer')
@click.option('--stack-name', type=click.STRING, required=True, help='OpsWorks Stack name')
@click.option('--layer-name', help='Layer to deploy application to')
@click.option('--comment', help='Deployment message')
@click.option('--timeout', default=None, help='Deployment timeout')
@click.option('--custom_json', default=None, help='Custom json filepath or native json string')
@click.pass_context
def rolling(ctx, stack_name, layer_name, comment, timeout, custom_json):
    operation = ctx.obj['OPERATION']
    operation.init(stack_name=stack_name, layer_name=layer_name, timeout=timeout)
    operation.layer_rolling(comment=comment, custom_json=custom_json)


@cli.command(help='Execute operation on specific hosts')
@click.option('--stack-name', type=click.STRING, required=True, help='OpsWorks Stack name')
@click.option('--hosts', '-H', help='Host names to deploy application to (comma separated list)')
@click.option('--comment', help='Deployment message')
@click.option('--timeout', default=None, help='Deployment timeout')
@click.option('--custom_json', default=None, help='Custom json filepath or native json string')
@click.pass_context
def instances(ctx, stack_name, hosts, comment, timeout, custom_json):
    operation = ctx.obj['OPERATION']
    operation.init(stack_name=stack_name, timeout=timeout)
    hosts = hosts.split(',')
    operation.instances_at_once(comment=comment, host_names=hosts, custom_json=custom_json)


if __name__ == '__main__':
    cli(obj={})
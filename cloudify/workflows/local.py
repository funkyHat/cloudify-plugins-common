########
# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#    * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    * See the License for the specific language governing permissions and
#    * limitations under the License.


import os
import tempfile
import copy
import importlib
import uuid
import json
import threading
import shutil

from cloudify_rest_client.nodes import Node
from cloudify_rest_client.node_instances import NodeInstance

from cloudify.workflows.workflow_context import (
    DEFAULT_LOCAL_TASK_THREAD_POOL_SIZE)

try:
    from dsl_parser import parser as dsl_parser, tasks as dsl_tasks
    from dsl_parser import functions as dsl_functions
    from dsl_parser import utils as dsl_utils
except ImportError:
    dsl_parser = None
    dsl_tasks = None
    dsl_functions = None
    dsl_utils = None


class Environment(object):

    def __init__(self,
                 blueprint_path,
                 name='local',
                 inputs=None,
                 storage_cls=None,
                 **storage_kwargs):

        if dsl_parser is None:
            raise ImportError('cloudify-dsl-parser must be installed to '
                              'execute local workflows. '
                              '(e.g. "pip install cloudify-dsl-parser")')

        self.name = name

        self.plan = dsl_tasks.prepare_deployment_plan(
            dsl_parser.parse_from_path(blueprint_path), inputs=inputs)

        nodes = [Node(node) for node in self.plan['nodes']]
        node_instances = [NodeInstance(instance)
                          for instance in self.plan['node_instances']]

        self._prepare_nodes_and_instances(nodes, node_instances)

        storage_kwargs.update(dict(
            name=self.name,
            resources_root=os.path.dirname(os.path.abspath(blueprint_path)),
            nodes=nodes,
            node_instances=node_instances
        ))

        if storage_cls is None:
            storage_cls = InMemoryStorage
        if storage_cls is Storage or not issubclass(storage_cls, Storage):
            raise ValueError('class {} must strictly derive from '
                             'Storage. [see InMemoryStorage and FileStorage]'
                             .format(storage_cls.__name__))

        self.storage = storage_cls(**storage_kwargs)

    def outputs(self):
        context = {}

        def handler(dict_, k, v, _):
            func = dsl_functions.parse(v)
            if isinstance(func, dsl_functions.GetAttribute):
                attributes = []
                if 'instances' not in context:
                    instances = self.storage.get_node_instances()
                    context['instances'] = instances
                for instance in context['instances']:
                    if instance.node_id == func.node_name:
                        runtime_properties = instance.runtime_properties or {}
                        attributes.append(runtime_properties.get(
                            func.attribute_name))
                dict_[k] = attributes

        outputs = copy.deepcopy(self.plan['outputs'])
        dsl_utils.scan_properties(outputs,
                                  handler,
                                  '{0}.outputs'.format(self.name))
        return outputs

    def execute(self,
                workflow,
                parameters=None,
                allow_custom_parameters=False,
                task_retries=-1,
                task_retry_interval=30,
                task_thread_pool_size=DEFAULT_LOCAL_TASK_THREAD_POOL_SIZE):
        workflows = self.plan['workflows']
        workflow_name = workflow
        if workflow_name not in workflows:
            raise ValueError("'{}' workflow does not exist. "
                             "existing workflows are: {}"
                             .format(workflow_name,
                                     workflows.keys()))

        workflow = workflows[workflow_name]
        workflow_method = self._get_module_method(workflow['operation'],
                                                  node_name='',
                                                  tpe='workflow')
        execution_id = str(uuid.uuid4())
        ctx = {
            'local': True,
            'deployment_id': self.name,
            'blueprint_id': self.name,
            'execution_id': execution_id,
            'workflow_id': workflow_name,
            'storage': self.storage,
            'task_retries': task_retries,
            'task_retry_interval': task_retry_interval,
            'local_task_thread_pool_size': task_thread_pool_size
        }

        merged_parameters = self._merge_and_validate_execution_parameters(
            workflow, workflow_name, parameters, allow_custom_parameters)

        workflow_method(__cloudify_context=ctx, **merged_parameters)

    def _prepare_nodes_and_instances(self, nodes, node_instances):

        def scan(parent, name, node):
            for operation in parent.get(name, {}).values():
                self._get_module_method(operation['operation'],
                                        tpe=name,
                                        node_name=node.id)

        for node in nodes:
            if 'relationships' not in node:
                node['relationships'] = []
            scan(node, 'operations', node)
            for relationship in node['relationships']:
                scan(relationship, 'source_operations', node)
                scan(relationship, 'target_operations', node)

        for node_instance in node_instances:
            node_instance['node_id'] = node_instance['name']
            if 'relationships' not in node_instance:
                node_instance['relationships'] = []

    @staticmethod
    def _get_module_method(module_method_path, tpe, node_name):
        split = module_method_path.split('.')
        module_name = '.'.join(split[:-1])
        method_name = split[-1]
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            raise ImportError('mapping error: No module named {} '
                              '[node={}, type={}]'
                              .format(module_name, node_name, tpe))
        try:
            return getattr(module, method_name)
        except AttributeError:
            raise AttributeError("mapping error: {} has no attribute '{}' "
                                 "[node={}, type={}]"
                                 .format(module.__name__, method_name,
                                         node_name, tpe))

    @staticmethod
    def _merge_and_validate_execution_parameters(
            workflow, workflow_name, execution_parameters=None,
            allow_custom_parameters=False):

        merged_parameters = {}
        workflow_parameters = workflow.get('parameters', {})
        execution_parameters = execution_parameters or {}

        missing_mandatory_parameters = set()

        for name, param in workflow_parameters.iteritems():
            if 'default' not in param:
                if name not in execution_parameters:
                    missing_mandatory_parameters.add(name)
                    continue
                merged_parameters[name] = execution_parameters[name]
            else:
                merged_parameters[name] = execution_parameters[name] if \
                    name in execution_parameters else param['default']

        if missing_mandatory_parameters:
            raise ValueError(
                'Workflow "{0}" must be provided with the following '
                'parameters to execute: {1}'
                .format(workflow_name, ','.join(missing_mandatory_parameters)))

        custom_parameters = dict(
            (k, v) for (k, v) in execution_parameters.iteritems()
            if k not in workflow_parameters)

        if not allow_custom_parameters and custom_parameters:
            raise ValueError(
                'Workflow "{0}" does not have the following parameters '
                'declared: {1}. Remove these parameters or use '
                'the flag for allowing custom parameters'
                .format(workflow_name, ','.join(custom_parameters.keys())))

        merged_parameters.update(custom_parameters)
        return merged_parameters


class Storage(object):

    def __init__(self, name, resources_root, nodes, node_instances):
        self.name = name
        self.resources_root = resources_root
        self._nodes = dict((
            node.id, node) for node in nodes)
        self._node_instances = dict((
            instance.id, instance) for instance in node_instances)
        for instance in self._node_instances.values():
            instance['version'] = 0
        self._locks = dict((
            instance_id, threading.RLock()) for instance_id
            in self._instance_ids())

    def get_resource(self, resource_path):
        with open(os.path.join(self.resources_root, resource_path)) as f:
            return f.read()

    def download_resource(self, resource_path, target_path=None):
        if not target_path:
            suffix = '-{}'.format(os.path.basename(resource_path))
            target_path = tempfile.mktemp(suffix=suffix)
        resource = self.get_resource(resource_path)
        with open(target_path, 'w') as f:
            f.write(resource)
        return target_path

    def get_node_instance(self, node_instance_id):
        raise NotImplementedError()

    def update_node_instance(self,
                             node_instance_id,
                             version,
                             runtime_properties=None,
                             state=None):
        with self._lock(node_instance_id):
            instance = self._get_node_instance(node_instance_id)
            if state is None and version != instance['version']:
                raise StorageConflictError('version {} does not match '
                                           'current version of '
                                           'node instance {} which is {}'
                                           .format(version,
                                                   node_instance_id,
                                                   instance['version']))
            else:
                instance['version'] += 1
            if runtime_properties is not None:
                instance['runtime_properties'] = runtime_properties
            if state is not None:
                instance['state'] = state
            self._store_instance(instance)

    def _get_node_instance(self, node_instance_id):
        instance = self._load_instance(node_instance_id)
        if instance is None:
            raise RuntimeError('Instance {} does not exist'
                               .format(node_instance_id))
        return instance

    def _load_instance(self, node_instance_id):
        raise NotImplementedError()

    def _store_instance(self, node_instance):
        raise NotImplementedError()

    def get_node(self, node_id):
        node = self._nodes.get(node_id)
        if node is None:
            raise RuntimeError('Node {} does not exist'
                               .format(node_id))
        return copy.deepcopy(node)

    def get_nodes(self):
        return copy.deepcopy(self._nodes.values())

    def get_node_instances(self):
        raise NotImplementedError()

    def _instance_ids(self):
        raise NotImplementedError()

    def _lock(self, node_instance_id):
        return self._locks[node_instance_id]


class InMemoryStorage(Storage):

    def __init__(self, name, resources_root, nodes, node_instances):
        super(InMemoryStorage, self).__init__(name,
                                              resources_root,
                                              nodes,
                                              node_instances)

    def get_node_instance(self, node_instance_id):
        return copy.deepcopy(self._get_node_instance(node_instance_id))

    def _load_instance(self, node_instance_id):
        return self._node_instances.get(node_instance_id)

    def _store_instance(self, node_instance):
        pass

    def get_node_instances(self):
        return copy.deepcopy(self._node_instances.values())

    def _instance_ids(self):
        return self._node_instances.keys()


class FileStorage(Storage):

    def __init__(self, name, resources_root, nodes, node_instances,
                 storage_dir='/tmp/cloudify-workflows',
                 clear=False):
        self._storage_dir = os.path.join(storage_dir, name)
        self._instances_dir = os.path.join(self._storage_dir,
                                           'node-instances')
        if os.path.isdir(self._storage_dir) and clear:
            shutil.rmtree(self._storage_dir)
        super(FileStorage, self).__init__(name,
                                          resources_root,
                                          nodes,
                                          node_instances)
        if not os.path.isdir(self._storage_dir):
            os.makedirs(self._storage_dir)
        if not os.path.isdir(self._instances_dir):
            os.mkdir(self._instances_dir)
            for instance in self._node_instances.values():
                self._store_instance(instance, lock=False)
        self._node_instances = None

    def get_node_instance(self, node_instance_id):
        return self._get_node_instance(node_instance_id)

    def _load_instance(self, node_instance_id):
        with self._lock(node_instance_id):
            with open(self._instance_path(node_instance_id)) as f:
                return NodeInstance(json.loads(f.read()))

    def _store_instance(self, node_instance, lock=True):
        if lock:
            instance_lock = self._lock(node_instance.id)
            instance_lock.acquire()
        try:
            with open(self._instance_path(node_instance.id), 'w') as f:
                f.write(json.dumps(node_instance))
        finally:
            if lock:
                instance_lock.release()

    def _instance_path(self, node_instance_id):
        return os.path.join(self._instances_dir, node_instance_id)

    def get_node_instances(self):
        return [self._get_node_instance(instance_id)
                for instance_id in self._instance_ids()]

    def _instance_ids(self):
        if os.path.isdir(self._instances_dir):
            return os.listdir(self._instances_dir)
        else:
            # only called during construction and when the directory does
            # not already exist.
            return self._node_instances.keys()


class StorageConflictError(Exception):
    pass

#!/usr/bin/python

import atexit
import json
import urllib
import os
import requests
from pyVim.connect import SmartConnectNoSSL, Disconnect
from ansible.module_utils.basic import *
from pyVmomi import vim, vmodl

__author__ = 'chaitanyaavi'


def is_vm_exist(si, cl, vm_name):
    container = si.content.viewManager.CreateContainerView(
         cl, [vim.VirtualMachine], True)
    for managed_object_ref in container.view:
        if managed_object_ref.name == vm_name:
            return True
    return False


def get_vm_by_name(si, vm_name):
    container = si.content.viewManager.CreateContainerView(
        si.content.rootFolder, [vim.VirtualMachine], True)
    for vm in container.view:
        if vm.name == vm_name:
            return vm
    return None


def get_dc(si, name):
    """
    Get a datacenter by its name.
    """
    for dc in si.content.rootFolder.childEntity:
        if dc.name == name:
            return dc
    raise Exception('Failed to find datacenter named %s' % name)


def compile_folder_path_for_object(vobj):
    """ make a /vm/foo/bar/baz like folder path for an object """
    paths = []
    if isinstance(vobj, vim.Folder):
        paths.append(vobj.name)

    thisobj = vobj
    while hasattr(thisobj, 'parent'):
        thisobj = thisobj.parent
        if isinstance(thisobj, vim.Folder):
            paths.append(thisobj.name)
    paths.reverse()
    if paths[0] == 'Datacenters':
        paths.remove('Datacenters')
    return '/' + '/'.join(paths)


def get_folder_by_path(si, dc, path):
    container = si.content.viewManager.CreateContainerView(
        dc, [vim.Folder], True)
    for managed_object_ref in container.view:
        if managed_object_ref.name == path.split("/")[-1:][0]:
            if path in compile_folder_path_for_object(managed_object_ref):
                return managed_object_ref
    return None


def get_cluster(si, dc, name):
    """
    Get a cluster in the datacenter by its names.
    """
    view_manager = si.content.viewManager
    container_view = view_manager.CreateContainerView(
        dc, [vim.ClusterComputeResource], True)
    try:
        for rp in container_view.view:
            if rp.name == name:
                return rp
    finally:
        container_view.Destroy()
    raise Exception("Failed to find cluster %s in datacenter %s" %
                    (name, dc.name))


def get_first_cluster(si, dc):
    """
    Get the first cluster in the list.
    """
    view_manager = si.content.viewManager
    container_view = view_manager.CreateContainerView(
        dc, [vim.ClusterComputeResource], True)
    try:
        first_cluster = container_view.view[0]
    finally:
        container_view.Destroy()
    if first_cluster is None:
        raise Exception("Failed to find a resource pool in dc %s" % dc.name)
    return first_cluster


def get_ds(dc, name):
    """
    Pick a datastore by its name.
    """
    for ds in dc.datastore:
        try:
            if ds.name == name:
                return ds
        except:  # Ignore datastores that have issues
            pass
    raise Exception("Failed to find %s on datacenter %s" % (name, dc.name))


def get_sysadmin_key(keypath):
    if os.path.exists(keypath):
        with open(keypath, 'r') as keyfile:
            data = keyfile.read().rstrip('\n')
            return data
    raise Exception('Failed to find sysadmin public key file at %s\n' % keypath)


def get_largest_free_ds(cl):
    """
    Pick the datastore that is accessible with the largest free space.
    """
    largest = None
    largest_free = 0
    for ds in cl.datastore:
        try:
            free_space = ds.summary.freeSpace
            if free_space > largest_free and ds.summary.accessible:
                largest_free = free_space
                largest = ds
        except:  # Ignore datastores that have issues
            pass
    if largest is None:
        raise Exception('Failed to find any free datastores on %s' % cl.name)
    return largest


def wait_for_tasks(service_instance, tasks):
    """Given the service instance si and tasks, it returns after all the
   tasks are complete
   """
    property_collector = service_instance.content.propertyCollector
    task_list = [str(task) for task in tasks]
    # Create filter
    obj_specs = [vmodl.query.PropertyCollector.ObjectSpec(obj=task)
                 for task in tasks]
    property_spec = vmodl.query.PropertyCollector.PropertySpec(type=vim.Task,
                                                               pathSet=[],
                                                               all=True)
    filter_spec = vmodl.query.PropertyCollector.FilterSpec()
    filter_spec.objectSet = obj_specs
    filter_spec.propSet = [property_spec]
    pcfilter = property_collector.CreateFilter(filter_spec, True)
    try:
        version, state = None, None
        # Loop looking for updates till the state moves to a completed state.
        while len(task_list):
            update = property_collector.WaitForUpdates(version)
            for filter_set in update.filterSet:
                for obj_set in filter_set.objectSet:
                    task = obj_set.obj
                    for change in obj_set.changeSet:
                        if change.name == 'info':
                            state = change.val.state
                        elif change.name == 'info.state':
                            state = change.val
                        else:
                            continue

                        if not str(task) in task_list:
                            continue

                        if state == vim.TaskInfo.State.success:
                            # Remove task from taskList
                            task_list.remove(str(task))
                        elif state == vim.TaskInfo.State.error:
                            raise task.info.error
            # Move to next version
            version = update.version
    finally:
        if pcfilter:
            pcfilter.Destroy()


def get_vm_ips(target_vm):
    ip_address = []
    for nic in target_vm.guest.net:
        addresses = nic.ipConfig.ipAddress
        for adr in addresses:
            ip_address.append(adr.ipAddress)
    return ip_address


def is_update_cpu(module):
    return ('con_number_of_cpus' in module.params and
            module.params['con_number_of_cpus'] is not None)


def is_update_memory(module):
    return ('con_memory' in module.params and
            module.params['con_memory'] is not None)


def is_reserve_memory(module):
    return ('con_memory_reserved' in module.params and
            module.params['con_memory_reserved'] is not None)


def is_reserve_cpu(module):
    return ('con_cpu_reserved' in module.params and
            module.params['con_cpu_reserved'] is not None)


def is_resize_disk(module):
    return ('con_disk_size' in module.params and
            module.params['con_disk_size'] is not None)


def is_reconfigure_vm(module):
    return (is_update_cpu(module) or is_update_memory(module) or
            is_reserve_memory(module)or is_reserve_cpu(module) or
            is_resize_disk(module))


def main():
    module = AnsibleModule(
        argument_spec=dict(
            ovftool_path=dict(required=True, type='str'),
            vcenter_host=dict(required=True, type='str'),
            vcenter_user=dict(required=True, type='str'),
            vcenter_password=dict(required=True, type='str', no_log=True),
            ssl_verify=dict(required=False, type='bool', default=False),
            state=dict(required=False, type='str', default='present'),
            con_datacenter=dict(required=False, type='str'),
            con_cluster=dict(required=False, type='str'),
            con_datastore=dict(required=False, type='str'),
            con_mgmt_network=dict(required=True, type='str'),
            con_disk_mode=dict(required=False, type='str', default='thin'),
            con_ova_path=dict(required=True, type='str'),
            con_vm_name=dict(required=True, type='str'),
            con_power_on=dict(required=False, type='bool', default=True),
            con_vcenter_folder=dict(required=False, type='str'),
            con_mgmt_ip=dict(required=False, type='str'),
            con_mgmt_mask=dict(required=False, type='str'),
            con_default_gw=dict(required=False, type='str'),
            con_sysadmin_public_key=dict(required=False, type='str'),
            con_number_of_cpus=dict(required=False, type='int'),
            con_cpu_reserved=dict(required=False, type='int'),
            con_memory=dict(required=False, type='int'),
            con_memory_reserved=dict(required=False, type='int'),
            con_disk_size=dict(required=False, type='int'),
            con_ovf_properties=dict(required=False, type='dict')
        ),
        supports_check_mode=True,
    )
    try:
        si = SmartConnectNoSSL(host=module.params['vcenter_host'],
                               user=module.params['vcenter_user'],
                               pwd=module.params['vcenter_password'])
        atexit.register(Disconnect, si)
    except vim.fault.InvalidLogin:
        return module.fail_json(
            msg='exception while connecting to vCenter, login failure, '
                'check username and password')
    except requests.exceptions.ConnectionError:
        return module.fail_json(
            msg='exception while connecting to vCenter, check hostname, '
                'FQDN or IP')
    check_mode = module.check_mode
    if module.params['state'] == 'absent':
        vm = get_vm_by_name(si, module.params['con_vm_name'])

        if vm is None:
            return module.exit_json(msg='A VM with the name %s not found' % (
                module.params['con_vm_name']))

        if check_mode:
            return module.exit_json(msg='A VM with the name %s found' % (
                module.params['con_vm_name']), changed=True)

        if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
            task = vm.PowerOffVM_Task()
            wait_for_tasks(si, [task])

        task = vm.Destroy_Task()
        wait_for_tasks(si, [task])

        return module.exit_json(msg='A VM with the name %s deleted successfully'
                                    % (module.params['con_vm_name']))

    if module.params.get('con_datacenter', None):
        dc = get_dc(si, module.params['con_datacenter'])
    else:
        dc = si.content.rootFolder.childEntity[0]

    if module.params.get('con_cluster', None):
        cl = get_cluster(si, dc, module.params['con_cluster'])
    else:
        cl = get_first_cluster(si, dc)

    if module.params.get('con_datastore', None):
        ds = get_ds(cl, module.params['con_datastore'])
    else:
        ds = get_largest_free_ds(cl)

    if is_vm_exist(si, cl, module.params['con_vm_name']):
        vm = get_vm_by_name(si, module.params['con_vm_name'])
        vm_path = compile_folder_path_for_object(vm)
        folder = get_folder_by_path(si, dc, module.params['con_vcenter_folder'])
        folder_path = compile_folder_path_for_object(folder)
        changed = False
        if vm_path != folder_path:
            # migrate vm to new folder
            if not check_mode:
                folder.MoveInto([vm])
            changed = True
        if (not module.params['con_power_on']) and \
                vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
            if not check_mode:
                task = vm.PowerOffVM_Task()
                wait_for_tasks(si, [task])
            changed = True
        if module.params['con_power_on'] and vm.runtime.powerState == \
                vim.VirtualMachinePowerState.poweredOff:
            if not check_mode:
                task = vm.PowerOnVM_Task()
                wait_for_tasks(si, [task])
            changed = True

        if module.params.get('con_datastore', None):
            ds_names = []
            for datastore in vm.datastore:
                ds_names.append(datastore.name)
            if ds.name not in ds_names:
                module.fail_json(msg='VM datastore cant be modified')

        if module.params.get('con_mgmt_ip', None):
            ip_addresses = get_vm_ips(vm)
            if (ip_addresses and
                    not module.params['con_mgmt_ip'] in ip_addresses):
                module.fail_json(msg='VM static ip address cant be modified')
        if changed and not check_mode:
            module.exit_json(msg='A VM with the name %s updated successfully' %
                                 (module.params['con_vm_name']), changed=True)
        if changed and check_mode:
            module.exit_json(changed=True)
        else:
            module.exit_json(
                msg='A VM with the name %s is already present' % (
                    module.params['con_vm_name']))

    if (not os.path.isfile(module.params['con_ova_path']) or
            not os.access(module.params['con_ova_path'], os.R_OK)):
        module.fail_json(msg='Controller OVA not found or not readable')

    ovftool_exec = '%s/ovftool' % module.params['ovftool_path']
    ova_file = module.params['con_ova_path']
    quoted_vcenter_user = urllib.quote(module.params['vcenter_user'])
    vi_string = 'vi://%s:%s@%s' % (
        quoted_vcenter_user, module.params['vcenter_password'],
        module.params['vcenter_host'])
    vi_string += '/%s/host/%s' % (dc.name, cl.name)
    command_tokens = [ovftool_exec]

    if module.params['con_power_on'] and not is_reconfigure_vm(module):
        command_tokens.append('--powerOn')
    if not module.params['ssl_verify']:
        command_tokens.append('--noSSLVerify')
    if check_mode:
        command_tokens.append('--verifyOnly')
    command_tokens.extend([
        '--acceptAllEulas',
        '--skipManifestCheck',
        '--allowExtraConfig',
        '--diskMode=%s' % module.params['con_disk_mode'],
        '--datastore=%s' % ds.name,
        '--name=%s' % module.params['con_vm_name']
    ])

    if ('ovf_network_name' in module.params.keys() and
            module.params['ovf_network_name'] is not None and
            len(module.params['ovf_network_name']) > 0):
            try:
                d = json.loads(
                    module.params['ovf_network_name'].replace("'", "\""))
                for key, network_item in d.iteritems():
                    command_tokens.append('--net:%s=%s' % (key, network_item))
            except ValueError:
                command_tokens.append('--net:%s=%s' % (
                    module.params['ovf_network_name'],
                    module.params['con_mgmt_network']))
    else:
        command_tokens.append(
            '--network=%s' % module.params['con_mgmt_network'])

    if module.params.get('con_mgmt_ip', None):
        command_tokens.append('--prop:%s=%s' % (
            'avi.mgmt-ip.CONTROLLER', module.params['con_mgmt_ip']))

    if module.params.get('con_mgmt_mask', None):
        command_tokens.append('--prop:%s=%s' % (
            'avi.mgmt-mask.CONTROLLER', module.params['con_mgmt_mask']))

    if module.params.get('con_default_gw', None):
        command_tokens.append('--prop:%s=%s' % (
            'avi.default-gw.CONTROLLER', module.params['con_default_gw']))

    if module.params.get('con_sysadmin_public_key', None):
        command_tokens.append('--prop:%s=%s' % (
            'avi.sysadmin-public-key.CONTROLLER',
            get_sysadmin_key(module.params['con_sysadmin_public_key'])))

    if module.params.get('con_ovf_properties', None):
        for key in module.params['con_ovf_properties'].keys():
            command_tokens.append(
                '--prop:%s=%s' % (
                    key, module.params['con_ovf_properties'][key]))

    if ('con_vcenter_folder' in module.params and
            module.params['con_vcenter_folder'] is not None):
        command_tokens.append(
            '--vmFolder=%s' % module.params['con_vcenter_folder'])

    command_tokens.extend([ova_file, vi_string])
    ova_tool_result = module.run_command(command_tokens)

    if ova_tool_result[0] != 0:
        return module.fail_json(
            msg='Failed to deploy OVA, error message from ovftool is: %s '
                'for command %s' % (ova_tool_result[1], command_tokens))

    if is_reconfigure_vm(module):
        vm = get_vm_by_name(si, module.params['con_vm_name'])
        cspec = vim.vm.ConfigSpec()
        if is_update_cpu(module):
            cspec.numCPUs = module.params['con_number_of_cpus']
        if is_update_memory(module):
            cspec.memoryMB = module.params['con_memory']
        if is_reserve_memory(module):
            cspec.memoryAllocation = vim.ResourceAllocationInfo(
                reservation=module.params['con_memory_reserved'])
        if is_reserve_cpu(module):
            cspec.cpuAllocation = vim.ResourceAllocationInfo(
                reservation=module.params['con_cpu_reserved'])
        if is_resize_disk(module):
            disk = None
            for device in vm.config.hardware.device:
                if isinstance(device, vim.vm.device.VirtualDisk):
                    disk = device
                    break
            if disk is not None:
                disk.capacityInKB = module.params['con_disk_size'] * 1024 * 1024
                devSpec = vim.vm.device.VirtualDeviceSpec(
                    device=disk, operation="edit")
                cspec.deviceChange.append(devSpec)
        wait_for_tasks(si, [vm.Reconfigure(cspec)])

        task = vm.PowerOnVM_Task()
        wait_for_tasks(si, [task])

    return module.exit_json(changed=True, ova_tool_result=ova_tool_result)


if __name__ == "__main__":
    main()

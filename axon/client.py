import json
import click
import boto3
import ipify
import webbrowser
import os.path
import axon.progress_reporter

all_perm = {
    "FromPort": -1,
    "IpProtocol": "-1",
    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    "Ipv6Ranges": [{"CidrIpv6": "::/0"}],
    "ToPort": -1
}

all_http_perm = {
    "FromPort": 80,
    "IpProtocol": "tcp",
    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    "Ipv6Ranges": [{"CidrIpv6": "::/0"}],
    "ToPort": 80
}


def make_client(name, region):
    if region is None:
        return boto3.client(name)
    else:
        return boto3.client(name, region_name=region)


def ensure_log_group(group_name, region):
    """
    Ensures that a log group is present. If there is a matching log group, nothing is created.
    If there is no matching log group, one is created.

    :param group_name: The name of the log group.
    :param region: The region, or `None` to pull the region from the environment.
    :return: Nothing.
    """
    client = make_client("logs", region)
    matching_log_groups = client.describe_log_groups(
        logGroupNamePrefix=group_name
    )
    log_groups = matching_log_groups["logGroups"]
    log_group_names = [it["logGroupName"] for it in log_groups]

    if group_name in log_group_names:
        # The log group exists, nothing else to do
        return
    else:
        # The log group needs to be created
        client.create_log_group(logGroupName=group_name)
        return


def revoke_all_perms(sg):
    """
    Revokes all permissions from the SecurityGroup.

    :param sg: The SecurityGroup.
    """
    if len(sg.ip_permissions) > 0:
        sg.revoke_ingress(IpPermissions=sg.ip_permissions)

    if len(sg.ip_permissions_egress) > 0:
        sg.revoke_egress(IpPermissions=sg.ip_permissions_egress)


def ensure_ecs_gress(sg_id, region):
    """
    Rewrites the ingress and egress permissions for the SecurityGroup. All existing ingress and
    egress permissions are revoked. The permissions that Axon needs are authorized.

    :param sg_id: The SecurityGroup's GroupId.
    :param region: The region, or `None` to pull the region from the environment.
    :return: Nothing.
    """
    ec2 = boto3.resource('ec2', region_name=region)
    sg = ec2.SecurityGroup(sg_id)

    revoke_all_perms(sg)

    ip = ipify.get_ip()
    axon_tcp_perm = {
        "FromPort": 8080,
        "IpProtocol": "tcp",
        "IpRanges": [{"CidrIp": "{}/32".format(ip)}],
        "ToPort": 8080
    }

    sg.authorize_egress(IpPermissions=[all_perm])
    sg.authorize_ingress(IpPermissions=[axon_tcp_perm])


def ensure_ec2_gress(sg_id, region):
    """
    Rewrites the ingress and egress permissions for the SecurityGroup. All existing ingress and
    egress permissions are revoked. The permissions that Axon needs are authorized.

    :param sg_id: The SecurityGroup's GroupId.
    :param region: The region, or `None` to pull the region from the environment.
    :return: Nothing.
    """
    ec2 = boto3.resource('ec2', region_name=region)
    sg = ec2.SecurityGroup(sg_id)

    revoke_all_perms(sg)

    sg.authorize_egress(IpPermissions=[all_perm])
    sg.authorize_ingress(IpPermissions=[all_http_perm])


def get_single_security_group(client, sg_name, desc):
    """
    Ensures that exactly one matching SecurityGroup exists. If there is one match, its permissions
    are remade. If there is more than one match, a RuntimeError is raised. If there are no matches,
    a new SecurityGroup is made.

    :param client: The EC2 client to use.
    :param sg_name: The name of the SecurityGroup.
    :param desc: The description of the SecurityGroup, if it needs to be created.
    :return: The GroupId of the matching SecurityGroup.
    """
    security_groups = client.describe_security_groups(
        Filters=[
            {
                "Name": "group-name",
                "Values": [sg_name]
            }
        ]
    )["SecurityGroups"]

    sgs = [it for it in security_groups if it["GroupName"] == sg_name]
    if len(sgs) > 1:
        raise RuntimeError("Matched multiple security groups: {}".format(sgs))

    if len(sgs) == 1:
        # The SG already exists
        sg = sgs[0]
        sg_id = sg["GroupId"]
    else:
        sg_id = client.create_security_group(
            Description=desc,
            GroupName=sg_name
        )["GroupId"]

    return sg_id


def ensure_ecs_security_group(region):
    """
    Ensures that the ECS SecurityGroup exists.
    :param region: The region, or `None` to pull the region from the environment.
    :return: The GroupId of the SecurityGroup.
    """
    sg_name = "axon-ecs-autogenerated"
    client = make_client("ec2", region)
    sg_id = get_single_security_group(client, sg_name, "Axon autogenerated for ECS.")
    ensure_ecs_gress(sg_id, region)
    return sg_id


def ensure_ec2_security_group(region):
    """
    Ensures that the EC2 SecurityGroup exists.
    :param region: The region, or `None` to pull the region from the environment.
    :return: The GroupId of the SecurityGroup.
    """
    sg_name = "axon-ec2-autogenerated"
    client = make_client("ec2", region)
    sg_id = get_single_security_group(client, sg_name, "Axon autogenerated for EC2.")
    ensure_ec2_gress(sg_id, region)
    return sg_id


def select_subnet(region):
    """
    Picks the first available subnet.
    :param region: The region, or `None` to pull the region from the environment.
    :return: The SubnetId.
    """
    client = make_client("ec2", region)
    return client.describe_subnets(Filters=[])["Subnets"][0]["SubnetId"]


def ensure_role(client, role_name):
    """
    Ensures that a SINGLE matching IAM role exists. Throws a `RuntimeError` if there are multiple
    matching roles.

    :param client: The iam client to use.
    :param role_name: The name of the IAM role.
    :return: The ARN of the matching IAM role, or `None` if there was no matching role.
    """
    roles = client.list_roles(PathPrefix="/")["Roles"]
    matching_roles = [it for it in roles if it["RoleName"] == role_name]
    if len(matching_roles) == 1:
        return matching_roles[0]["Arn"]
    elif len(matching_roles) > 1:
        raise RuntimeError("Found multiple matching roles: {}".format(role_name, roles))
    else:
        return None


def ensure_task_role(region):
    """
    Ensures a task role exists. If there is one matching role, its Arn is returned. If there are
    multiple matching roles, a RuntimeError is raised. If there are no matching roles, a new one
    is created.

    TODO: Fix this.
    This method does not check that a matching role has the correct policies.
    :param region: The region, or `None` to pull the region from the environment.
    :return: The role Arn.
    """
    role_name = "axon-ecs-autogenerated-task-role"
    client = make_client("iam", region)
    role_arn = ensure_role(client, role_name)
    if role_arn is None:
        # Need to create the role
        role = client.create_role(
            Path="/",
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "",
                        "Effect": "Allow",
                        "Principal": {
                            "Service": "ecs-tasks.amazonaws.com"
                        },
                        "Action": "sts:AssumeRole"
                    }
                ]
            })
        )["Role"]

        role_arn = role["Arn"]

        client.attach_role_policy(RoleName=role_name,
                                  PolicyArn="arn:aws:iam::aws:policy/service-role/"
                                            "AmazonECSTaskExecutionRolePolicy")
        client.attach_role_policy(RoleName=role_name,
                                  PolicyArn="arn:aws:iam::aws:policy/AmazonEC2FullAccess")
        client.attach_role_policy(RoleName=role_name,
                                  PolicyArn="arn:aws:iam::aws:policy/AmazonS3FullAccess")
        client.attach_role_policy(RoleName=role_name,
                                  PolicyArn="arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess")
    return role_arn


def ensure_ec2_role(region):
    role_name = "axon-ec2-role-manual"
    client = make_client("iam", region)
    role_arn = ensure_role(client, role_name)
    if role_arn is None:
        # Need to create the role
        role = client.create_role(
            Path="/",
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {
                            "Service": "ec2.amazonaws.com"
                        },
                        "Action": "sts:AssumeRole"
                    }
                ]
            })
        )["Role"]

        role_arn = role["Arn"]

        client.attach_role_policy(RoleName=role_name,
                                  PolicyArn="arn:aws:iam::aws:policy/AmazonS3FullAccess")
    return role_arn


def ensure_cluster(ecs_client, cluster_name):
    """
    Ensures that a matching cluster exists. If there are no matching clusters, one is created.

    :param ecs_client: The ECS client to use.
    :param cluster_name: The simple name of the cluster.
    :return: Nothing.
    """
    clusters = ecs_client.describe_clusters(clusters=[cluster_name])["clusters"]
    if len([it for it in clusters if it["clusterName"] == cluster_name]) == 0:
        ecs_client.create_cluster(clusterName=cluster_name)


def ensure_task(ecs_client, task_family, region, vcpu, memory):
    """
    Ensures that a matching task definition exists. If there is at least one match, its Arn is
    returned. If there are no matches, a new task definition is created.

    This method does not check that a matching task definition has up-to-date values for the task
    role, cpu, memory, etc.

    :param ecs_client: The ECS client to use.
    :param task_family: The task family name.
    :param region: The region, or `None` to pull the region from the environment.
    :param vcpu: The amount of cpu in vcpu units.
    :param memory: The amount of memory in MB.
    :return: The task definition's Arn.
    """
    def_arns = ecs_client.list_task_definitions(
        familyPrefix=task_family,
        sort="DESC"
    )["taskDefinitionArns"]

    matching_arns = [it for it in def_arns if it.split("/")[-1].split(":")[0] == task_family]

    if len(matching_arns) != 0:
        # There is at least one matching task definition, so we don't need to create one
        return matching_arns[0]
    else:
        # There are no matching task definitions, so make one
        log_group_name = "/ecs/{}".format(task_family)
        ensure_log_group(log_group_name, region)

        role_arn = ensure_task_role(region)

        reg_response = ecs_client.register_task_definition(
            family=task_family,
            taskRoleArn=role_arn,
            executionRoleArn=role_arn,
            networkMode="awsvpc",
            containerDefinitions=[
                {
                    "name": "axon-hosted",
                    "image": "wpilib/axon-hosted",
                    "essential": True,
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {
                            "awslogs-group": log_group_name,
                            "awslogs-region": region,
                            "awslogs-stream-prefix": "ecs"
                        }
                    }
                }
            ],
            requiresCompatibilities=["FARGATE"],
            cpu=str(vcpu),
            memory=str(memory)
        )

        return reg_response["taskDefinition"]["taskDefinitionArn"]


def wait_for_task_to_start(task_arn, cluster, region):
    """
    Waits for a task to transition to the RUNNING state.

    :param task_arn: The Arn of the task to wait for.
    :param cluster: The simple name of the cluster the task is in.
    :param region: The region, or `None` to pull the region from the environment.
    :return: Nothing.
    """
    client = make_client("ecs", region)
    waiter = client.get_waiter("tasks_running")
    waiter.wait(cluster=cluster, tasks=[task_arn])


def impl_ensure_configuration(cluster_name, task_family, region):
    """
    Ensures all the configuration Axon needs is in place.

    :param cluster_name: The simple name of the cluster to start the task in.
    :param task_family: The family of the task to start.
    :param region: The region, or `None` to pull the region from the environment.
    """
    client = make_client("ecs", region)
    ensure_cluster(client, cluster_name)
    ensure_task(client, task_family, region, 2048, 4096)
    ensure_ec2_security_group(region)
    ensure_ecs_security_group(region)
    ensure_task_role(region)
    ensure_ec2_role(region)


def impl_start_task(cluster_name, task_family, revision, region):
    """
    Starts a task. Creates the cluster, task, and security group if they are not present. Selects
    the first available subnet.

    Raises a RuntimeError if the task failed to start or if more than one task was started.

    :param cluster_name: The simple name of the cluster to start the task in.
    :param task_family: The family of the task to start.
    :param revision: A task definition revision number, or None to use the latest revision.
    :param region: The region, or `None` to pull the region from the environment.
    :return: The started task's Arn.
    """
    client = make_client("ecs", region)

    impl_ensure_configuration(cluster_name, task_family, region)

    sg_id = ensure_ecs_security_group(region)
    subnet_id = select_subnet(region)

    run_response = client.run_task(
        cluster=cluster_name,
        taskDefinition=task_family if revision is None else "{}:{}".format(task_family, revision),
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": [subnet_id],
                "securityGroups": [sg_id],
                "assignPublicIp": "ENABLED"
            }
        }
    )

    tasks = run_response["tasks"]

    if len(tasks) == 0:
        raise RuntimeError("Failed to start task: {}".format(run_response))
    if len(tasks) > 1:
        raise RuntimeError("Started more than one task: {}".format(tasks))
    else:
        return tasks[0]["taskArn"]


def impl_stop_task(cluster_name, task_arn, region):
    """
    Stops a task.

    :param cluster_name: The simple name of the cluster.
    :param task_arn: The Arn of the task.
    :param region: The region, or `None` to pull the region from the environment.
    :return: Nothing.
    """
    client = make_client("ecs", region)
    client.stop_task(cluster=cluster_name, task=task_arn)


def impl_get_task_ip(cluster_name, task_arn, region):
    """
    Reads the public IP of a task. This is probably only valid for tasks that have one container.

    :param cluster_name: The simple name of the cluster.
    :param task_arn: The task's Arn.
    :param region: The region, or `None` to pull the region from the environment.
    :return: The public IP of the task.
    """
    client = make_client("ecs", region)
    task_arn = client.describe_tasks(
        cluster=cluster_name,
        tasks=[task_arn]
    )["tasks"][0]

    interface_attachment = next(
        x for x in task_arn["attachments"] if x["type"] == "ElasticNetworkInterface")
    eni = next(
        x["value"] for x in interface_attachment["details"] if x["name"] == "networkInterfaceId")

    ec2 = make_client("ec2", region)
    nics = ec2.describe_network_interfaces(
        Filters=[
            {
                "Name": "network-interface-id",
                "Values": [eni]
            }
        ]
    )["NetworkInterfaces"]
    return nics[0]["Association"]["PublicIp"]


def impl_upload_model_file(local_file_path, bucket_name, region):
    """
    Uploads a model to S3.

    :param local_file_path: The path to the model file on disk.
    :param bucket_name: The S3 bucket name.
    :param region: The region, or `None` to pull the region from the environment.
    """
    client = make_client("s3", region)
    remote_path = "axon-uploaded-trained-models/" + os.path.basename(local_file_path)
    client.upload_file(local_file_path, bucket_name, remote_path)
    print("Uploaded to: {}\n".format(remote_path))


def impl_download_model_file(local_file_path, bucket_name, region):
    """
    Downloads a model from S3.

    :param local_file_path: The path to the model file on disk.
    :param bucket_name: The S3 bucket name.
    :param region: The region, or `None` to pull the region from the environment.
    """
    client = make_client("s3", region)
    remote_path = "axon-uploaded-trained-models/" + os.path.basename(local_file_path)
    client.download_file(bucket_name, remote_path, local_file_path)
    print("Downloaded from: {}\n".format(remote_path))


def impl_download_training_script(local_script_path, bucket_name, region):
    """
    Downloads a training script from S3.

    :param local_script_path: The path to the training script on disk.
    :param bucket_name: The S3 bucket name.
    :param region: The region, or `None` to pull the region from the environment.
    """
    client = make_client("s3", region)
    remote_path = "axon-uploaded-training-scripts/" + os.path.basename(local_script_path)
    client.download_file(bucket_name, remote_path, local_script_path)
    print("Downloaded from: {}\n".format(remote_path))


def impl_download_dataset(local_dataset_path, bucket_name, region):
    """
    Downloads a dataset from S3.

    :param local_dataset_path: The path to the dataset on disk.
    :param bucket_name: The S3 bucket name.
    :param region: The region, or `None` to pull the region from the environment.
    """
    client = make_client("s3", region)
    remote_path = "axon-uploaded-datasets/" + os.path.basename(local_dataset_path)
    client.download_file(bucket_name, remote_path, local_dataset_path)
    print("Downloaded from: {}\n".format(remote_path))


@click.group()
def cli():
    return


# TODO: Don't set a default value for region in any of these

@cli.command(name="start-axon")
@click.argument("cluster-name")
@click.argument("task-family")
@click.option("--revision", default=None,
              help="The revision of the task. Set to None to use the latest revision.")
@click.option("--region", default="us-east-1", help="The region to connect to.")
def start_axon(cluster_name, task_family, revision, region):
    impl_ensure_configuration(cluster_name, task_family, region)
    task_arn = impl_start_task(cluster_name, task_family, revision, region)
    print("Started task: {}".format(task_arn))
    print("Waiting for task to start...")
    wait_for_task_to_start(task_arn, cluster_name, region)
    print("Started")
    ip = impl_get_task_ip(cluster_name, task_arn, region)
    # TODO: How do we wait for Axon to start running?
    webbrowser.open("http://{}:8080/axon/dataset".format(ip), 2)


@cli.command(name="ensure-configuration")
@click.argument("cluster-name")
@click.argument("task-family")
@click.option("--region", default="us-east-1", help="The region to connect to.")
def ensure_configuration(cluster_name, task_family, region):
    impl_ensure_configuration(cluster_name, task_family, region)


@cli.command(name="start-task")
@click.argument("cluster-name")
@click.argument("task-family")
@click.option("--revision", default=None,
              help="The revision of the task. Set to None to use the latest revision.")
@click.option("--region", default="us-east-1", help="The region to connect to.")
@click.option("--stop-after/--no-stop-after", default=False,
              help="Whether to stop the task immediately after creating it.")
def start_task(cluster_name, task_family, revision, region, stop_after):
    impl_ensure_configuration(cluster_name, task_family, region)
    task_arn = impl_start_task(cluster_name, task_family, revision, region)
    print("Started task: {}".format(task_arn))
    if stop_after:
        impl_stop_task(cluster_name, task_arn, region)
    else:
        print("Waiting for task to start...")
        wait_for_task_to_start(task_arn, cluster_name, region)
        print("Started")


@cli.command(name="stop-task")
@click.argument("cluster-name")
@click.argument("task")
@click.option("--region", default="us-east-1", help="The region to connect to.")
def stop_task(cluster_name, task, region):
    impl_stop_task(cluster_name, task, region)


@cli.command(name="get-container-ip")
@click.argument("cluster-name")
@click.argument("task")
@click.option("--region", default="us-east-1", help="The region to connect to.")
def get_container_ip(cluster_name, task, region):
    print(impl_get_task_ip(cluster_name, task, region))


@cli.command(name="upload-model-file")
@click.argument("local-file-path")
@click.argument("bucket-name")
@click.option("--region", default="us-east-1", help="The region to connect to.")
def upload_model_file(local_file_path, bucket_name, region):
    impl_upload_model_file(local_file_path, bucket_name, region)


@cli.command(name="download-model-file")
@click.argument("local-file-path")
@click.argument("bucket-name")
@click.option("--region", default="us-east-1", help="The region to connect to.")
def download_model_file(local_file_path, bucket_name, region):
    impl_download_model_file(local_file_path, bucket_name, region)


@cli.command(name="download-training-script")
@click.argument("local-script-path")
@click.argument("bucket-name")
@click.option("--region", default="us-east-1", help="The region to connect to.")
def download_training_script(local_script_path, bucket_name, region):
    impl_download_training_script(local_script_path, bucket_name, region)


@cli.command(name="download-dataset")
@click.argument("local-dataset-path")
@click.argument("bucket-name")
@click.option("--region", default="us-east-1", help="The region to connect to.")
def download_dataset(local_dataset_path, bucket_name, region):
    impl_download_dataset(local_dataset_path, bucket_name, region)

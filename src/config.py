#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for handling Kafka configuration."""

import logging
import socket
from typing import Dict, List, Optional

from ops.charm import CharmBase
from ops.model import Container, Unit

from literals import CHARM_KEY, PEER, REL_NAME, ZOOKEEPER_REL_NAME

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_OPTIONS = """
clientPort=2181
sasl.enabled.mechanisms=SCRAM-SHA-512
sasl.mechanism.inter.broker.protocol=SCRAM-SHA-512
listener.security.protocol.map=INTERNAL:SASL_PLAINTEXT,EXTERNAL:SASL_PLAINTEXT
inter.broker.listener.name=INTERNAL
authorizer.class.name=kafka.security.authorizer.AclAuthorizer
allow.everyone.if.no.acl.found=false
listener.name.internal.sasl.enabled.mechanisms=SCRAM-SHA-512
listener.name.external.sasl.enabled.mechanisms=SCRAM-SHA-512
"""


# listeners=SASL_PLAINTEXT://:9092


class KafkaConfig:
    """Manager for handling Kafka configuration."""

    def __init__(self, charm: CharmBase):
        self.charm = charm
        self.default_config_path = f"{self.charm.config['data-dir']}/config"
        self.properties_filepath = f"{self.default_config_path}/server.properties"
        self.jaas_filepath = f"{self.default_config_path}/kafka-jaas.cfg"

    @property
    def container(self) -> Container:
        """Grabs the current Kafka container."""
        return getattr(self.charm, "unit").get_container(CHARM_KEY)

    @property
    def sync_password(self) -> Optional[str]:
        """Returns charm-set sync_password for server-server auth between brokers."""
        return self.charm.model.get_relation(PEER).data[self.charm.app].get("sync_password", None)

    @property
    def zookeeper_config(self) -> Dict[str, str]:
        """Checks the zookeeper relations for data necessary to connect to ZooKeeper.

        Returns:
            Dict with zookeeper username, password, endpoints, chroot and uris
        """
        zookeeper_config = {}
        for relation in self.charm.model.relations[ZOOKEEPER_REL_NAME]:
            zk_keys = ["username", "password", "endpoints", "chroot", "uris"]
            missing_config = any(
                relation.data[relation.app].get(key, None) is None for key in zk_keys
            )

            if missing_config:
                continue

            zookeeper_config.update(relation.data[relation.app])
            break

        if zookeeper_config:
            sorted_uris = sorted(
                zookeeper_config["uris"].replace(zookeeper_config["chroot"], "").split(",")
            )
            sorted_uris[-1] = sorted_uris[-1] + zookeeper_config["chroot"]
            zookeeper_config["connect"] = ",".join(sorted_uris)

        return zookeeper_config

    @property
    def zookeeper_connected(self) -> bool:
        """Checks if there is an active ZooKeeper relation.

        Returns:
            True if ZooKeeper is currently related with sufficient relation data
                for a broker to connect with. False otherwise.
        """
        if self.zookeeper_config.get("connect", None):
            return True

        return False

    @property
    def extra_args(self) -> str:
        """Collection of Java config arguments for SASL auth.

        Returns:
            String of command argument to be prepended to start-up command
        """
        extra_args = f"-Djava.security.auth.login.config={self.jaas_filepath}"

        return extra_args

    @property
    def bootstrap_server(self) -> List[str]:
        """The current Kafka uris formatted for the `bootstrap-server` command flag.

        Returns:
            List of `bootstrap-server` servers
        """
        hosts = [self.get_host_from_unit(unit=unit) for unit in self.units]
        return [f"{host}:9092" for host in hosts]

    @property
    def kafka_command(self) -> str:
        """The run command for starting the Kafka service.

        Returns:
            String of startup command and expected config filepath
        """
        entrypoint = "/opt/kafka/bin/kafka-server-start.sh"
        return f"{entrypoint} {self.properties_filepath}"

    @property
    def default_replication_properties(self) -> List[str]:
        """Builds replication-related properties based on the expected app size.

        Returns:
            List of properties to be set
        """
        replication_factor = min([3, self.charm.app.planned_units()])
        min_isr = max([1, replication_factor - 1])

        return [
            f"default.replication.factor={replication_factor}",
            f"num.partitions={replication_factor}",
            f"transaction.state.log.replication.factor={replication_factor}",
            f"offsets.topic.replication.factor={replication_factor}",
            f"min.insync.replicas={min_isr}",
            f"transaction.state.log.min.isr={min_isr}",
        ]

    @property
    def units(self) -> List[Unit]:
        """Returns the list of units for the model.

        Returns:
            List of Unit
        """
        return list(set([self.charm.unit] + list(self.charm.model.get_relation(PEER).units)))

    @property
    def auth_properties(self) -> List[str]:
        """Builds properties necessary for inter-broker authorization through ZooKeeper.

        Returns:
            List of properties to be set
        """
        unit = self.charm.unit

        broker_id = unit.name.split("/")[1]

        listeners = ",".join(
            [
                f"INTERNAL://{self.get_host_from_unit(unit)}:29092",
                f"EXTERNAL://{self.get_ip_address_from_unit(unit)}:9092",
            ]
        )

        output = [
            f"broker.id={broker_id}",
            f"listeners={listeners}",
            f"advertised.listeners={listeners}",
            f'zookeeper.connect={self.zookeeper_config["connect"]}',
            f'listener.name.internal.scram-sha-512.sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username="sync" password="{self.sync_password}";',
            f'listener.name.external.scram-sha-512.sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username="sync" password="{self.sync_password}";',
        ]

        logger.info(output)

        return output

    @property
    def super_users(self) -> str:
        """Generates all users with super/admin permissions for the cluster from relations.

        Formatting allows passing to the `super.users` property.

        Returns:
            Semi-colon delimited string of current super users
        """
        super_users = ["sync"]
        for relation in self.charm.model.relations[REL_NAME]:
            extra_user_roles = relation.data[relation.app].get("extra-user-roles", "")
            password = (
                self.charm.model.get_relation(PEER)
                .data[self.charm.app]
                .get(f"relation-{relation.id}", None)
            )
            # if passwords are set for client admins, they're good to load
            if "admin" in extra_user_roles and password is not None:
                super_users.append(f"relation-{relation.id}")

        super_users_arg = [f"User:{user}" for user in super_users]

        return ";".join(super_users_arg)

    @property
    def server_properties(self) -> List[str]:
        """Builds all properties necessary for starting Kafka service.

        This includes charm config, replication, SASL/SCRAM auth and default properties.

        Returns:
            List of properties to be set
        """
        return (
            [
                f"data.dir={self.charm.config['data-dir']}",
                f"log.dir={self.charm.config['log-dir']}",
                f"offsets.retention.minutes={self.charm.config['offsets-retention-minutes']}",
                f"log.retention.hours={self.charm.config['log-retention-hours']}",
                f"auto.create.topics={self.charm.config['auto-create-topics']}",
                f"super.users={self.super_users}",
            ]
            + self.default_replication_properties
            + self.auth_properties
            + DEFAULT_CONFIG_OPTIONS.split("\n")
        )

    def push(self, content: str, path: str) -> None:
        """Wrapper for writing a file and contents to a container.

        Args:
            content: the text content to write to a file path
            path: the full path of the desired file
        """
        self.container.push(path, content, make_dirs=True)

    def set_server_properties(self) -> None:
        """Sets all kafka config properties to the `server.properties` path."""
        self.push(content="\n".join(self.server_properties), path=self.properties_filepath)

    @property
    def jaas(self) -> str:
        """Provide the JAAS setting via property."""
        return f"""
            Client {{
                org.apache.zookeeper.server.auth.DigestLoginModule required
                username="{self.zookeeper_config['username']}"
                password="{self.zookeeper_config['password']}";
            }};
        """

    def set_jaas_config(self) -> None:
        """Sets the Kafka JAAS config using zookeeper relation data."""
        self.push(content=self.jaas, path=self.jaas_filepath)

    def get_host_from_unit(self, unit: Unit) -> str:
        """Builds K8s host address for a given `Unit`.

        Args:
            unit: the desired unit

        Returns:
            String of host address
        """
        broker_id = unit.name.split("/")[1]

        return f"{self.charm.app.name}-{broker_id}.{self.charm.app.name}-endpoints"

    def get_ip_address_from_unit(self, unit: Unit) -> str:
        """Retrieve K8s host address for a given `Unit`.

        Args:
            unit: the desired unit

        Returns:
            String of host address
        """
        return socket.gethostbyname(self.get_host_from_unit(unit))

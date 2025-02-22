import os

from ansible.parsing.dataloader import DataLoader
from ansible.inventory.manager import InventoryManager

from cli.helpers.Step import Step
from cli.helpers.doc_list_helpers import select_single, select_all
from cli.helpers.build_saver import save_manifest, get_inventory_path, get_manifest_path, get_build_path
from cli.helpers.data_loader import load_manifest_docs
from cli.helpers.yaml_helpers import safe_load_all
from cli.helpers.Log import Log
from cli.helpers.os_images import get_os_distro_normalized
from cli.engine.providers.provider_class_loader import provider_class_loader
from cli.engine.schema.DefaultMerger import DefaultMerger
from cli.engine.schema.SchemaValidator import SchemaValidator
from cli.engine.schema.ConfigurationAppender import ConfigurationAppender
from cli.engine.terraform.TerraformTemplateGenerator import TerraformTemplateGenerator
from cli.engine.terraform.TerraformFileCopier import TerraformFileCopier
from cli.engine.terraform.TerraformRunner import TerraformRunner
from cli.engine.ansible.AnsibleRunner import AnsibleRunner


class ApplyEngine(Step):
    def __init__(self, input_data):
        super().__init__(__name__)
        self.file = input_data.file
        self.skip_infrastructure = getattr(input_data, 'no_infra', False)
        self.skip_config = getattr(input_data, 'skip_config', False)
        self.ansible_options = {'profile_tasks': getattr(input_data, 'profile_ansible_tasks', False)}
        self.logger = Log(__name__)

        self.cluster_model = None
        self.input_docs = []
        self.configuration_docs = []
        self.infrastructure_docs = []
        self.manifest_docs = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    def process_input_docs(self):
        # Load the user input YAML docs from the input file.
        if os.path.isabs(self.file):
            path_to_load = self.file
        else:
            path_to_load = os.path.join(os.getcwd(), self.file)
        user_file_stream = open(path_to_load, 'r')
        self.input_docs = safe_load_all(user_file_stream)

        # Merge the input docs with defaults
        with DefaultMerger(self.input_docs) as doc_merger:
            self.input_docs = doc_merger.run()

        # Get the cluster model.
        self.cluster_model = select_single(self.input_docs, lambda x: x.kind == 'epiphany-cluster')
        if self.cluster_model is None:
            raise Exception('No cluster model defined in input YAML file')

        # Validate cluster input document.
        # Other documents might need more processing (SET_BY_AUTOMATION) so will be validated at a later stage.
        with SchemaValidator(self.cluster_model.provider, [self.cluster_model]) as schema_validator:
            schema_validator.run()

    def process_infrastructure_docs(self):
        # Load any posible existing manifest docs
        self.load_manifest_docs()

        # Build the infrastructure docs
        with provider_class_loader(self.cluster_model.provider, 'InfrastructureBuilder')(
                self.input_docs, self.manifest_docs) as infrastructure_builder:
            self.infrastructure_docs = infrastructure_builder.run()

        # Validate infrastructure documents
        with SchemaValidator(self.cluster_model.provider, self.infrastructure_docs) as schema_validator:
            schema_validator.run()

    def process_configuration_docs(self):
        # Append with components and configuration docs
        with ConfigurationAppender(self.input_docs) as config_appender:
            self.configuration_docs = config_appender.run()

        # Validate configuration documents
        with SchemaValidator(self.cluster_model.provider, self.configuration_docs) as schema_validator:
            schema_validator.run()

    def collect_infrastructure_config(self):
        with provider_class_loader(self.cluster_model.provider, 'InfrastructureConfigCollector')(
                [*self.configuration_docs, *self.infrastructure_docs]) as config_collector:
            config_collector.run()

    def load_manifest_docs(self):
        path_to_manifest = get_manifest_path(self.cluster_model.specification.name)
        if os.path.isfile(path_to_manifest):
            self.manifest_docs = load_manifest_docs(get_build_path(self.cluster_model.specification.name))

    def assert_no_master_downscale(self):
        components = self.cluster_model.specification.components

        # Skip downscale assertion for single machine clusters
        if ('single_machine' in components) and (int(components['single_machine']['count']) > 0):
            return

        cluster_name = self.cluster_model.specification.name
        inventory_path = get_inventory_path(cluster_name)

        if os.path.isfile(inventory_path):
            existing_inventory = InventoryManager(loader=DataLoader(), sources=inventory_path)

            both_present = all([
                'kubernetes_master' in existing_inventory.list_groups(),
                'kubernetes_master' in components,
            ])

            if both_present:
                prev_master_count = len(existing_inventory.list_hosts(pattern='kubernetes_master'))
                next_master_count = int(components['kubernetes_master']['count'])

                if prev_master_count > next_master_count:
                    raise Exception("ControlPlane downscale is not supported yet. Please revert your 'kubernetes_master' count to previous value or increase it to scale up Kubernetes.")

    def assert_consistent_os_family(self):
        # Before this issue https://github.com/epiphany-platform/epiphany/issues/195 gets resolved,
        # we are forced to do assertion here.

        virtual_machine_docs = select_all(
            self.infrastructure_docs,
            lambda x: x.kind == 'infrastructure/virtual-machine',
        )

        os_indicators = {
            get_os_distro_normalized(vm_doc)
            for vm_doc in virtual_machine_docs
        }

        if len(os_indicators) > 1:
            raise Exception("Detected mixed Linux distros in config, Epirepo will not work properly. Please inspect your config manifest. Forgot to define repository VM document?")

    def apply(self):
        self.process_input_docs()

        self.assert_no_master_downscale()

        self.process_infrastructure_docs()

        save_manifest([*self.input_docs, *self.infrastructure_docs], self.cluster_model.specification.name)

        self.assert_consistent_os_family()

        if not (self.skip_infrastructure or self.is_provider_any(self.cluster_model)):
            # Generate terraform templates
            with TerraformTemplateGenerator(self.cluster_model, self.infrastructure_docs) as template_generator:
                template_generator.run()

            # Copy cloud-config.yml since it contains bash code which can't be templated easily (requires {% raw %}...{% endraw %})
            with TerraformFileCopier(self.cluster_model, self.infrastructure_docs) as file_copier:
                file_copier.run()

            # Run Terraform to create infrastructure
            with TerraformRunner(self.cluster_model, self.configuration_docs) as tf_runner:
                tf_runner.build()

        self.process_configuration_docs()

        self.collect_infrastructure_config()

        # Merge all the docs
        docs = [*self.configuration_docs, *self.infrastructure_docs]

        # Save docs to manifest file
        save_manifest(docs, self.cluster_model.specification.name)

        # Run Ansible to provision infrastructure
        if not(self.skip_config):
            with AnsibleRunner(self.cluster_model, docs, ansible_options=self.ansible_options) as ansible_runner:
                ansible_runner.apply()

        return 0

    def dry_run(self):

        self.process_input_docs()

        self.process_configuration_docs()

        return [*self.configuration_docs, *self.infrastructure_docs]

    @staticmethod
    def is_provider_any(cluster_model):
        return cluster_model["provider"] == "any"

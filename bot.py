"""
OCI Out of Capacity Fix
Version 2.1.4
Moses (@mosesman831)
GitHub: https://github.com/mosesman831/OCI-OcC-Fix
"""

import oci
import logging
import time
import sys
import telebot
import datetime
import configparser
import json
import requests
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional, List

# Constants
CONFIG_FILE = 'configuration.ini'
LOG_FILE = 'oci_occ.log'
MAX_LOG_SIZE = 2 * 1024 * 1024  # 2 MB
LOG_BACKUP_COUNT = 4  # 4 backups + current = 5 files total

class OciOccFix:
    def __init__(self):
        # Phase 1: Core configuration
        self.config = self.load_config()
        self.setup_logging()
        
        # Phase 2: Initialize critical parameters first
        self.wait_seconds = self.config.getint(
            'Retry', 
            'initial_retry_interval',
            fallback=1
        )
        
        # Phase 3: Service clients
        self.clients = self.initialize_oci_clients()
        
        # Phase 4: Telegram integration
        # Fixed execution order
        # self.tg_message_id = None
        # self.tg_bot = self.initialize_telegram()
        
        # Phase 5: Runtime state
        self.total_retries = 0
        self.retry_counter = 0
        self.last_status_notify_ts = time.time()

    @staticmethod
    def load_config() -> configparser.ConfigParser:
        """Load and validate configuration with strict checks"""
        config = configparser.ConfigParser()
        if not Path(CONFIG_FILE).exists():
            raise FileNotFoundError(f"Configuration file {CONFIG_FILE} not found")
        
        config.read(CONFIG_FILE)
        
        # Validate required sections
        required_sections = ['OCI', 'Instance', 'Telegram', 'Machine', 'Retry']
        for section in required_sections:
            if not config.has_section(section):
                raise ValueError(f"Missing required section: [{section}]")

        # Validate Retry parameters
        required_retry_keys = [
            'min_interval',
            'max_interval',
            'initial_retry_interval',
            'backoff_factor'
        ]
        for key in required_retry_keys:
            if not config.has_option('Retry', key):
                raise ValueError(f"Missing required Retry key: {key}")
                
        return config

    def setup_logging(self):
        """Configure logging with rotation and validation"""
        formatter = logging.Formatter('[%(levelname)s] %(asctime)s - %(message)s')
        log_level = self.config.get(
            'Logging', 
            'log_level', 
            fallback='INFO'
        ).upper()

        handlers = [
            RotatingFileHandler(
                LOG_FILE,
                maxBytes=MAX_LOG_SIZE,
                backupCount=LOG_BACKUP_COUNT,
                encoding='utf-8'
            ),
            logging.StreamHandler()
        ]

        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format=formatter._fmt,
            handlers=handlers
        )

    def initialize_oci_clients(self) -> Dict[str, object]:
        """Initialize OCI clients with error containment"""
        try:
            oci_config = oci.config.from_file('./config')
            return {
                'compute': oci.core.ComputeClient(oci_config),
                'identity': oci.identity.IdentityClient(oci_config),
                'network': oci.core.VirtualNetworkClient(oci_config),
                'blockstorage': oci.core.BlockstorageClient(oci_config)
            }
        except Exception as e:
            logging.error(f"OCI client initialization failed: {str(e)}")
            sys.exit(1)

    @staticmethod
    def format_response_data(data) -> str:
        """Serialize OCI response data for logging"""
        def serialize_item(item):
            if hasattr(item, 'to_dict'):
                return item.to_dict()
            return item

        if isinstance(data, list):
            serializable = [serialize_item(item) for item in data]
        else:
            serializable = serialize_item(data)

        try:
            return json.dumps(serializable, ensure_ascii=True, sort_keys=True)
        except TypeError:
            return str(serializable)

    def initialize_telegram(self) -> Optional[telebot.TeleBot]:
        """Initialize Telegram bot with safe defaults"""
        bot_token = self.config.get('Telegram', 'bot_token', fallback='')
        uid = self.config.get('Telegram', 'uid', fallback='')
        
        if not bot_token or bot_token == 'xxxx':
            return None
        if not uid or uid == 'xxxx':
            return None
            
        try:
            bot = telebot.TeleBot(bot_token)
            self.send_telegram_startup_message(bot)
            return bot
        except Exception as e:
            logging.warning(f"Telegram initialization failed: {str(e)}")
            return None

    def send_telegram_startup_message(self, bot: telebot.TeleBot):
        """Send startup message with enhanced error handling"""
        try:
            tenancy = self.clients['identity'].get_tenancy(
                self.config.get('OCI', 'compartment_id')
            ).data
            users = self.clients['identity'].list_users(
                self.config.get('OCI', 'compartment_id')
            ).data
            
            message = (
                "🚀 OCI-OcC-Fix Initialized\n"
                f"• Account: {tenancy.name}\n"
                f"• User: {users[0].email if users else 'Unknown'}\n"
                f"• Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"• Retry Interval: {self.wait_seconds}s\n"
                f"• Machine: {self.config.get('Machine', 'shape')}"
            )
            
            sent = bot.send_message(
                chat_id=self.config.get('Telegram', 'uid'),
                text=message
            )
            self.tg_message_id = sent.message_id
        except Exception as e:
            logging.error(f"Telegram startup message failed: {str(e)}")

    def validate_resources(self) -> bool:
        """Perform comprehensive resource validation with error handling"""
        try:
            compartment_id = self.config.get('OCI', 'compartment_id')
            total_storage = 0

            # Storage validation (updated for SDK 2.147.0)
            volumes = self.clients['blockstorage'].list_volumes(
                compartment_id=compartment_id
            ).data
            
            total_storage += sum(
                v.size_in_gbs 
                for v in volumes 
                if v.lifecycle_state not in ("TERMINATING", "TERMINATED")
            )

            # Boot volumes check (updated for SDK 2.147.0)
            ads = json.loads(self.config.get('OCI', 'availability_domains'))
            for ad in ads:
                boot_volumes = self.clients['blockstorage'].list_boot_volumes(
                    compartment_id=compartment_id,
                    availability_domain=ad.strip()
                ).data
                total_storage += sum(
                    bv.size_in_gbs 
                    for bv in boot_volumes 
                    if bv.lifecycle_state not in ("TERMINATING", "TERMINATED")
                )

            required_size = self.config.getint(
                'Instance', 
                'boot_volume_size', 
                fallback=47
            )
            if (200 - total_storage) < required_size:
                logging.critical(
                    f"Storage limit exceeded: {200 - total_storage}GB free < {required_size}GB needed"
                )
                return False

            # Instance validation (updated for SDK 2.147.0)
            instances = self.clients['compute'].list_instances(
                compartment_id=compartment_id
            ).data
            active_instances = [
                i for i in instances 
                if i.lifecycle_state not in ("TERMINATING", "TERMINATED")
            ]
            
            if self.config.get('Instance', 'display_name') in [i.display_name for i in active_instances]:
                logging.critical("Duplicate instance name detected")
                return False

            # ARM quota validation
            if self.config.get('Machine', 'type').upper() == 'ARM':
                arm_instances = [
                    i for i in active_instances 
                    if i.shape == "VM.Standard.A1.Flex"
                ]
                total_ocpus = sum(i.shape_config.ocpus for i in arm_instances)
                total_memory = sum(i.shape_config.memory_in_gbs for i in arm_instances)
                
                new_ocpus = self.config.getint('Machine', 'ocpus')
                new_memory = self.config.getint('Machine', 'memory')
                
                if (total_ocpus + new_ocpus) > 4 or (total_memory + new_memory) > 24:
                    logging.critical("ARM quota exceeded: Max 4 OCPUs/24GB")
                    return False

            return True

        except Exception as e:
            logging.error(f"Resource validation failed: {str(e)}")
            return False

    def create_instance(self, availability_domain: str) -> Optional[str]:
        """Create instance with robust error handling"""
        try:
            launch_details = oci.core.models.LaunchInstanceDetails(
                metadata={
                    "ssh_authorized_keys": self.config.get('Instance', 'ssh_keys')
                },
                availability_domain=availability_domain.strip(),
                compartment_id=self.config.get('OCI', 'compartment_id'),
                shape=self.config.get('Machine', 'shape'),
                display_name=self.config.get('Instance', 'display_name'),
                source_details=self.get_source_details(),
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=self.config.get('OCI', 'subnet_id'),
                    assign_public_ip=True
                ),
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=self.config.getint('Machine', 'ocpus'),
                    memory_in_gbs=self.config.getint('Machine', 'memory')
                )
            )

            response = self.clients['compute'].launch_instance(
                launch_instance_details=launch_details
            )
            logging.info(
                f"Launch instance response: {self.format_response_data(response.data)}"
            )
            return response.data.id
        except oci.exceptions.ServiceError as e:
            logging.warning(
                f"Create failed in {availability_domain}: {e.code} - {e.message}"
            )
            self.adaptive_retry_wait(e.code)
            return None
        except Exception as e:
            logging.error(f"Unexpected creation error: {str(e)}")
            self.send_weixin_update(
                "❌ Create instance error",
                f"• AD: {availability_domain}\n"
                f"• Error: {str(e)}"
            )
            return None

    def get_source_details(self):
        """Get source config with fallback handling"""
        if self.config.get('OCI', 'boot_volume_id', fallback='xxxx') != 'xxxx':
            return oci.core.models.InstanceSourceViaBootVolumeDetails(
                source_type="bootVolume",
                boot_volume_id=self.config.get('OCI', 'boot_volume_id')
            )
        
        return oci.core.models.InstanceSourceViaImageDetails(
            source_type="image",
            image_id=self.config.get('OCI', 'image_id'),
            boot_volume_size_in_gbs=self.config.getint(
                'Instance', 
                'boot_volume_size',
                fallback=47
            )
        )

    def handle_success(self, instance_id: str):
        """Handle successful creation with IP retrieval"""
        try:
            vnic = self.clients['compute'].list_vnic_attachments(
                compartment_id=self.config.get('OCI', 'compartment_id'),
                instance_id=instance_id
            )
            logging.info(
                f"List VNIC attachments response: {self.format_response_data(vnic.data)}"
            )
            vnic = vnic.data[0]

            private_ip = self.clients['network'].list_private_ips(
                vnic_id=vnic.vnic_id
            )
            logging.info(
                f"List private IPs response: {self.format_response_data(private_ip.data)}"
            )
            private_ip = private_ip.data[0].id

            public_ip = self.clients['network'].get_public_ip_by_private_ip_id(
                get_public_ip_by_private_ip_id_details=oci.core.models.GetPublicIpByPrivateIpIdDetails(
                    private_ip_id=private_ip
                )
            )
            logging.info(
                f"Get public IP response: {self.format_response_data(public_ip.data)}"
            )
            public_ip = public_ip.data.ip_address

            logging.info(f"✅ Instance created! Public IP: {public_ip}")
            self.send_weixin_update(
                "🚀 Instance Ready!",
                f"• IP: {public_ip}\n"
                f"• Retries: {self.total_retries}\n"
                f"• Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
            sys.exit(0)

        except Exception as e:
            logging.error(f"Success handling failed: {str(e)}")
            self.send_weixin_update(
                "❌ Success handler error",
                f"• Instance ID: {instance_id}\n"
                f"• Error: {str(e)}"
            )
            sys.exit(1)

    def send_telegram_update(self, message: str):
        """Update Telegram message with error handling"""
        if not self.tg_bot or not self.tg_message_id:
            return

        try:
            self.tg_bot.edit_message_text(
                chat_id=self.config.get('Telegram', 'uid'),
                message_id=self.tg_message_id,
                text=message
            )
        except Exception as e:
            logging.warning(f"Telegram update failed: {str(e)}")

    def send_weixin_update(self, title: str, text: str = ""):
        """Send Weixin bot message with error handling"""
        bot_url = self.config.get('Weixin', 'bot_url', fallback='')
        if not bot_url or bot_url == 'xxxx':
            return

        content = f"{title}\n{text}".strip()
        data = json.dumps(
            {
                "msgtype": "text",
                "text": {"content": content}
            },
            ensure_ascii=True
        )
        headers = {'Content-Type': 'application/json'}

        try:
            response = requests.post(
                bot_url,
                headers=headers,
                data=data,
                timeout=10
            )
            logging.info(f"Weixin notify response: {response.text}")
        except Exception as e:
            logging.warning(f"Weixin notify failed: {str(e)}")

    def get_status_interval_seconds(self) -> int:
        """Get status notification interval in seconds"""
        if self.config.has_section('Notify') and self.config.has_option(
            'Notify', 'status_interval_minutes'
        ):
            minutes = self.config.getint('Notify', 'status_interval_minutes')
        else:
            minutes = 30

        return max(minutes, 0) * 60

    def maybe_send_status_update(self, ad: str):
        """Send periodic status update based on elapsed time"""
        interval_seconds = self.get_status_interval_seconds()
        if interval_seconds <= 0:
            return

        now = time.time()
        if now - self.last_status_notify_ts < interval_seconds:
            return

        self.send_weixin_update(
            f"🔁 Attempt {self.total_retries}",
            f"• Last Error: {ad} capacity\n"
            f"• Next retry: {self.wait_seconds:.1f}s\n"
            f"• Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self.last_status_notify_ts = now

    def adaptive_retry_wait(self, error_code: str):
        """Adjust retry timing with bounds checking"""
        min_interval = self.config.getint('Retry', 'min_interval', fallback=1)
        max_interval = self.config.getint('Retry', 'max_interval', fallback=60)
        backoff_factor = self.config.getfloat('Retry', 'backoff_factor', fallback=1.5)

        if error_code == 'TooManyRequests':
            self.wait_seconds = min(
                self.wait_seconds * backoff_factor,
                max_interval
            )
        else:
            self.wait_seconds = max(
                self.wait_seconds / 1.5,
                min_interval
            )

        # Ensure wait stays within configured bounds
        self.wait_seconds = max(min(self.wait_seconds, max_interval), min_interval)
        logging.info(f"⏳ Next retry in {self.wait_seconds:.1f}s")

    def run(self):
        """Main execution loop with enhanced error handling"""
        if not self.validate_resources():
            logging.critical("❌ Resource validation failed")
            sys.exit(1)

        ads = json.loads(self.config.get('OCI', 'availability_domains'))
        
        while True:
            try:
                for ad in ads:
                    self.total_retries += 1
                    instance_id = self.create_instance(ad)
                    
                    if instance_id:
                        self.handle_success(instance_id)
                    
                    # Update status on a time interval
                    self.maybe_send_status_update(ad)

                    time.sleep(self.wait_seconds)

            except KeyboardInterrupt:
                logging.info("🛑 Process interrupted by user")
                self.send_weixin_update(
                    "🛑 Process interrupted",
                    "Process interrupted by user"
                )
                sys.exit(0)
            except Exception as e:
                error_code = getattr(e, 'code', 'Unknown')
                logging.error(f"⚠️ Unexpected error: {str(e)}")
                self.adaptive_retry_wait(error_code)
                time.sleep(self.wait_seconds)

if __name__ == "__main__":
    try:
        OciOccFix().run()
    except Exception as e:
        logging.critical(f"💀 Fatal initialization error: {str(e)}")
        sys.exit(1)

"""
OCI Out of Capacity Fix
Version v2.2.0
Moses (@mosesman831)
GitHub: https://github.com/mosesman831/OCI-OcC-Fix
"""

import argparse
import oci
import logging
import time
import sys
import telebot
import datetime
import configparser
import json
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional, List

# Constants
CONFIG_FILE = 'configuration.ini'
OCI_CONFIG_FILE = 'config'
LOG_FILE = 'oci_occ.log'
MAX_LOG_SIZE = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3
RETRYABLE_ERROR_CODES = {"TooManyRequests", "OutOfHostCapacity", "OutOfCapacity"}

class OciOccFix:
    def __init__(self, config_path: Path, oci_config_path: Path):
        self.config_path = config_path
        self.oci_config_path = oci_config_path
        # Phase 1: Core configuration
        self.config = self.load_config(self.config_path)
        self.setup_logging()
        
        # Phase 2: Initialize critical parameters first
        self.wait_seconds = self.config.getint(
            'Retry', 
            'initial_retry_interval',
            fallback=1
        )
        
        # Phase 3: Service clients
        self.clients = self.initialize_oci_clients()
        
        # Phase 4: Notification channels (Telegram + WeChat Work coexist)
        self.tg_message_id = None
        self.tg_bot = self.initialize_telegram()
        self.wx_url = self.initialize_weixin()
        self.last_status_time = time.monotonic()
        self.send_startup_notifications()

        # Phase 5: Runtime state
        self.total_retries = 0
        self.retry_counter = 0

    @staticmethod
    def load_config(config_path: Path) -> configparser.ConfigParser:
        """Load and validate configuration with strict checks"""
        config = configparser.ConfigParser()
        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file {config_path} not found")
        
        config.read(config_path)
        
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
            self.oci_config = oci.config.from_file(str(self.oci_config_path))
            return {
                'compute': oci.core.ComputeClient(self.oci_config),
                'identity': oci.identity.IdentityClient(self.oci_config),
                'network': oci.core.VirtualNetworkClient(self.oci_config),
                'blockstorage': oci.core.BlockstorageClient(self.oci_config)
            }
        except Exception as e:
            logging.error(f"OCI client initialization failed: {str(e)}")
            sys.exit(1)

    def initialize_telegram(self) -> Optional[telebot.TeleBot]:
        """Initialize Telegram bot with safe defaults"""
        bot_token = self.config.get('Telegram', 'bot_token', fallback='')
        uid = self.config.get('Telegram', 'uid', fallback='')
        
        if not bot_token or bot_token == 'xxxx':
            return None
        if not uid or uid == 'xxxx':
            return None
            
        try:
            return telebot.TeleBot(bot_token)
        except Exception as e:
            logging.warning(f"Telegram initialization failed: {str(e)}")
            return None

    def initialize_weixin(self) -> Optional[str]:
        """Enable WeChat Work webhook notifications if a URL is configured"""
        bot_url = self.config.get('Weixin', 'bot_url', fallback='')
        if not bot_url or bot_url == 'xxxx':
            return None
        return bot_url

    def send_weixin(self, message: str) -> None:
        """Send a text message to the WeChat Work webhook (best-effort)"""
        if not self.wx_url:
            return
        try:
            payload = json.dumps(
                {"msgtype": "text", "text": {"content": message}}
            ).encode("utf-8")
            req = urllib.request.Request(
                self.wx_url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("errcode") != 0:
                logging.warning(
                    f"WeChat notify failed: errcode={body.get('errcode')} "
                    f"errmsg={body.get('errmsg')}"
                )
        except Exception as e:
            logging.warning(f"WeChat notify failed: {str(e)}")

    def build_startup_message(self) -> str:
        """Compose the startup notification text"""
        account = user = 'Unknown'
        try:
            tenancy = self.clients['identity'].get_tenancy(
                self.oci_config['tenancy']
            ).data
            users = self.clients['identity'].list_users(
                compartment_id=self.oci_config['tenancy']
            ).data
            account = tenancy.name
            user = users[0].email if users else 'Unknown'
        except Exception as e:
            logging.error(f"Failed to fetch account info for startup message: {str(e)}")

        return (
            "🚀 OCI-OcC-Fix Initialized\n"
            f"• Account: {account}\n"
            f"• User: {user}\n"
            f"• Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"• Retry Interval: {self.wait_seconds}s\n"
            f"• Machine: {self.config.get('Machine', 'shape')}"
        )

    def send_startup_notifications(self) -> None:
        """Send the startup message to every configured channel"""
        if not self.tg_bot and not self.wx_url:
            return
        message = self.build_startup_message()
        # Telegram keeps an editable message handle for live in-place updates
        if self.tg_bot:
            try:
                sent = self.tg_bot.send_message(
                    self.config.get('Telegram', 'uid'), message
                )
                self.tg_message_id = sent.message_id
            except Exception as e:
                logging.error(f"Telegram startup message failed: {str(e)}")
        # WeChat Work webhook only supports posting new messages (no edit)
        self.send_weixin(message)

    def notify_update(self, message: str) -> None:
        """Broadcast a status update to all configured channels"""
        self.send_telegram_update(message)
        self.send_weixin(message)

    def validate_resources(self) -> bool:
        """Perform comprehensive resource validation with error handling"""
        try:
            compartment_id = self.config.get('OCI', 'compartment_id')
            total_storage = 0

            # Storage validation
            volumes = self.clients['blockstorage'].list_volumes(compartment_id=compartment_id).data
            total_storage += sum(
                v.size_in_gbs 
                for v in volumes 
                if v.lifecycle_state not in ("TERMINATING", "TERMINATED")
            )

            # Boot volumes check
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

            # Instance validation
            instances = self.clients['compute'].list_instances(compartment_id=compartment_id).data
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
                    assign_public_ip=False
                ),
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=self.config.getint('Machine', 'ocpus'),
                    memory_in_gbs=self.config.getint('Machine', 'memory')
                )
            )

            response = self.clients['compute'].launch_instance(launch_details)
            return response.data.id
        except oci.exceptions.ServiceError as e:
            error_code = e.code
            error_code_display = error_code or 'UnknownServiceError'
            logging.warning(
                f"Create failed in {availability_domain}: {error_code_display} - {e.message}"
            )
            if error_code in RETRYABLE_ERROR_CODES:
                self.adaptive_retry_wait(error_code)
            return None
        except Exception as e:
            logging.error(f"Unexpected creation error: {str(e)}")
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
            ).data[0]

            private_ip_obj = self.clients['network'].list_private_ips(
                vnic_id=vnic.vnic_id
            ).data[0]
            private_ip = private_ip_obj.ip_address

            # The instance is created without a public IP (assign_public_ip=False),
            # so this lookup is best-effort: attach one manually later if needed.
            public_ip = None
            try:
                public_ip = self.clients['network'].get_public_ip_by_private_ip_id(
                    oci.core.models.GetPublicIpByPrivateIpIdDetails(
                        private_ip_id=private_ip_obj.id
                    )
                ).data.ip_address
            except Exception:
                pass

            ip_line = (
                f"Public IP: {public_ip}" if public_ip
                else f"Private IP: {private_ip} (no public IP - attach manually)"
            )
            logging.info(f"✅ Instance created! {ip_line}")
            self.notify_update(
                f"🚀 Instance Ready!\n"
                f"• {ip_line}\n"
                f"• Retries: {self.total_retries}\n"
                f"• Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
            sys.exit(0)

        except Exception as e:
            logging.error(f"Success handling failed: {str(e)}")
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
        status_interval = self.config.getint(
            'Notify', 'status_interval_minutes', fallback=30
        )

        while True:
            try:
                for ad in ads:
                    self.total_retries += 1
                    instance_id = self.create_instance(ad)

                    if instance_id:
                        self.handle_success(instance_id)

                    # Periodic progress update, throttled by status_interval_minutes
                    if status_interval > 0 and (
                        time.monotonic() - self.last_status_time
                    ) >= status_interval * 60:
                        self.last_status_time = time.monotonic()
                        self.notify_update(
                            f"🔁 Attempt {self.total_retries}\n"
                            f"• Last Error: {ad} capacity\n"
                            f"• Next retry: {self.wait_seconds:.1f}s"
                        )

                    time.sleep(self.wait_seconds)

            except KeyboardInterrupt:
                logging.info("🛑 Process interrupted by user")
                self.notify_update("🛑 Process interrupted by user")
                sys.exit(0)
            except Exception as e:
                error_code = getattr(e, 'code', 'Unknown')
                logging.error(f"⚠️ Unexpected error: {str(e)}")
                self.adaptive_retry_wait(error_code)
                time.sleep(self.wait_seconds)

def main() -> None:
    parser = argparse.ArgumentParser(description="OCI-OcC-Fix runner")
    parser.add_argument(
        "--config",
        default=CONFIG_FILE,
        help="Path to configuration.ini",
    )
    parser.add_argument(
        "--oci-config",
        default=OCI_CONFIG_FILE,
        help="Path to OCI SDK config file (default: ./config)",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    oci_config_path = Path(args.oci_config).expanduser().resolve()

    try:
        OciOccFix(config_path=config_path, oci_config_path=oci_config_path).run()
    except Exception as e:
        logging.critical(f"💀 Fatal initialization error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()

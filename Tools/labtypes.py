# labtypes.py - HOLFY27 LabType Execution Path Manager
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Manages different startup sequences for HOL, Discovery, VXP, ATE, EDU lab types

import os
import importlib.util
from typing import List, Optional, Dict, Any

class LabTypeLoader:
    """
    Manages lab-type specific startup module loading and execution.
    
    Lab Types:
    - HOL: Hands-on Labs - Full production with firewall and proxy filtering
    - Discovery: Discovery Labs - Simplified, no firewall
    - VXP: VCF Experience Program - Demo environments
    - ATE: Advanced Technical Enablement (Livefire) - Instructor-led labs
    - EDU: Education - Training environments
    """
    
    # Define startup sequences for each lab type
    # Keys are UPPERCASE to match the normalization in __init__
    STARTUP_SEQUENCE: Dict[str, List[str]] = {
        'HOL': [
            'prelim',
            'ESXi',
            'VCF',
            'VVF',
            'vSphere',
            'pings',
            'services',
            'Kubernetes',
            'urls',
            'VCFfinal',
            'final',
            'odyssey'
        ],
        'DISCOVERY': [
            'prelim',
            'ESXi',
            'VCF',
            'VVF',
            'vSphere',
            'pings',
            'services',
            'Kubernetes',
            'urls',
            'VCFfinal',
            'final',
            'odyssey'
        ],
        'VXP': [
            'prelim',
            'ESXi',
            'VCF',
            'VVF',
            'vSphere',
            'pings',
            'services',
            'Kubernetes',
            'urls',
            'VCFfinal',
            'final',
            'odyssey'
        ],
        'ATE': [
            'prelim',
            'ESXi',
            'VCF',
            'VVF',
            'vSphere',
            'pings',
            'services',
            'Kubernetes',
            'urls',
            'VCFfinal',
            'final',
            'odyssey'
        ],
        'EDU': [
            'prelim',
            'ESXi',
            'VCF',
            'VVF',
            'vSphere',
            'pings',
            'services',
            'Kubernetes',
            'urls',
            'VCFfinal',
            'final',
            'odyssey'
        ]
    }
    
    # Lab type descriptions and configuration
    # Keys are UPPERCASE to match the normalization in __init__
    # repo_pattern: 'standard' = PREFIX-XXYY (year-based), 'named' = PREFIX-Name (no year extraction)
    LABTYPE_INFO: Dict[str, Dict[str, Any]] = {
        'HOL': {
            'name': 'Hands-on Labs',
            'description': 'Full production labs with firewall, proxy filtering',
            'firewall': True,
            'proxy_filter': True,
            'repo_pattern': 'standard'
        },
        'DISCOVERY': {
            'name': 'Discovery Labs',
            'description': 'Simplified labs, no firewall restrictions',
            'firewall': False,
            'proxy_filter': False,
            'repo_pattern': 'named'
        },
        'VXP': {
            'name': 'VCF Experience Program',
            'description': 'Demo environments for VCF Experience',
            'firewall': True,
            'proxy_filter': True,
            'repo_pattern': 'standard'
        },
        'ATE': {
            'name': 'Advanced Technical Enablement',
            'description': 'Advanced instructor-led Livefire labs',
            'firewall': True,
            'proxy_filter': False,
            'repo_pattern': 'standard'
        },
        'EDU': {
            'name': 'Education',
            'description': 'Training environments',
            'firewall': True,
            'proxy_filter': True,
            'repo_pattern': 'standard'
        }
    }
    
    def __init__(self, labtype: str, holroot: str, vpod_repo: str = ''):
        """
        Initialize the LabType loader
        
        :param labtype: Lab type (HOL, Discovery, VXP, ATE, EDU)
        :param holroot: Core team repository root path
        :param vpod_repo: Lab-specific vpodrepo path
        """
        self.labtype = labtype.upper() if labtype else 'HOL'
        self.holroot = holroot
        self.vpod_repo = vpod_repo
        
        # Validate lab type
        if self.labtype not in self.STARTUP_SEQUENCE:
            print(f'WARNING: Unknown labtype {self.labtype}, defaulting to HOL')
            self.labtype = 'HOL'
    
    def get_labtype_info(self) -> Dict[str, Any]:
        """Get information about the current lab type"""
        return self.LABTYPE_INFO.get(self.labtype, self.LABTYPE_INFO['HOL'])
    
    def requires_firewall(self) -> bool:
        """Check if this lab type requires firewall"""
        return self.get_labtype_info().get('firewall', True)
    
    def requires_proxy_filter(self) -> bool:
        """Check if this lab type requires proxy filtering"""
        return self.get_labtype_info().get('proxy_filter', True)
    
    def get_repo_pattern(self) -> str:
        """
        Get the repository naming pattern for this lab type.
        
        Returns:
            'standard': PREFIX-XXYY format (year-based, e.g., HOL-2701, ATE-2705)
            'named': PREFIX-Name format (no year extraction, e.g., Discovery-Demo)
        """
        return self.get_labtype_info().get('repo_pattern', 'standard')
    
    def get_module_path(self, module_name: str) -> Optional[str]:
        """
        Find the path to a startup module, respecting override hierarchy:
        
        1. /vpodrepo/20XX-labs/XXXX/Startup/{module}.py  (Highest - vpodrepo Startup override)
        2. /vpodrepo/20XX-labs/XXXX/{module}.py          (vpodrepo root override)
        3. /home/holuser/hol/Startup.{labtype}/{module}.py  (LabType-specific core)
        4. /home/holuser/hol/Startup/{module}.py         (Default core module)
        
        :param module_name: Name of the module (without .py)
        :return: Full path to the module, or None if not found
        """
        filename = f'{module_name}.py'
        
        search_paths = [
            # VPodRepo overrides (highest priority)
            os.path.join(self.vpod_repo, 'Startup', filename),
            os.path.join(self.vpod_repo, filename),
            
            # LabType-specific core
            os.path.join(self.holroot, f'Startup.{self.labtype}', filename),
            
            # Default core (lowest priority)
            os.path.join(self.holroot, 'Startup', filename)
        ]
        
        for path in search_paths:
            if os.path.isfile(path):
                return path
        
        return None
    
    def load_module(self, module_name: str):
        """
        Dynamically load a startup module
        
        :param module_name: Name of the module
        :return: Tuple of (module, module_path)
        """
        module_path = self.get_module_path(module_name)
        
        if not module_path:
            raise FileNotFoundError(f"Module {module_name} not found for labtype {self.labtype}")
        
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        
        return module, module_path
    
    def get_startup_sequence(self) -> List[str]:
        """Get the startup sequence for current labtype"""
        return self.STARTUP_SEQUENCE.get(self.labtype, self.STARTUP_SEQUENCE['HOL'])
    
    def run_startup(self, lsf):
        """
        Execute the complete startup sequence for the labtype
        
        :param lsf: lsfunctions module reference
        :raises Exception: If a critical module fails
        """
        sequence = self.get_startup_sequence()
        
        lsf.write_output(f'Starting {self.labtype} startup sequence: {sequence}')
        
        # Define which modules are critical (failure should stop the sequence)
        critical_modules = ['prelim', 'ESXi', 'VCF', 'VCFfinal']
        
        for module_name in sequence:
            module_path = self.get_module_path(module_name)
            
            if module_path:
                lsf.write_output(f'Running {module_name} from {module_path}')
                
                try:
                    result = lsf.startup(module_name)
                    
                    # Check if module reported failure
                    if result is False:
                        lsf.write_output(f'Module {module_name} returned failure status')
                        if module_name in critical_modules:
                            raise RuntimeError(f'Critical module {module_name} failed')
                        
                except Exception as e:
                    lsf.write_output(f'Module {module_name} failed: {e}')
                    # Continue with next module unless it's a critical failure
                    if module_name in critical_modules:
                        raise
            else:
                lsf.write_output(f'Skipping {module_name} - not found')
    
    def list_available_modules(self) -> Dict[str, str]:
        """
        List all available modules and their paths
        
        :return: Dictionary of module_name -> path
        """
        sequence = self.get_startup_sequence()
        modules = {}
        
        for module_name in sequence:
            path = self.get_module_path(module_name)
            modules[module_name] = path if path else 'NOT FOUND'
        
        return modules


def get_labtype_from_config(config) -> str:
    """
    Get labtype from config parser
    
    :param config: ConfigParser instance
    :return: Lab type string
    """
    if config.has_option('VPOD', 'labtype'):
        return config.get('VPOD', 'labtype').upper()
    return 'HOL'


# For standalone testing
if __name__ == '__main__':
    import sys
    
    # Test with different lab types
    for lt in ['HOL', 'Discovery', 'VXP', 'ATE', 'EDU']:
        print(f'\n=== {lt} ===')
        loader = LabTypeLoader(
            labtype=lt,
            holroot='/home/holuser/hol',
            vpod_repo='/vpodrepo/2027-labs/2701'
        )
        
        info = loader.get_labtype_info()
        print(f"Name: {info['name']}")
        print(f"Description: {info['description']}")
        print(f"Firewall: {loader.requires_firewall()}")
        print(f"Proxy Filter: {loader.requires_proxy_filter()}")
        print(f"Sequence: {loader.get_startup_sequence()}")

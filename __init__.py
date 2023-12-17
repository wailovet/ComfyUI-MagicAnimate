import folder_paths
import os
import sys
sys.dont_write_bytecode = True

sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), "libs"))

os.makedirs(os.path.join(folder_paths.models_dir, "MagicAnimate"), exist_ok=True)

folder_paths.add_model_folder_path("magic_animate", os.path.join(folder_paths.models_dir, "MagicAnimate"))
folder_paths.folder_names_and_paths['magic_animate'] = (folder_paths.folder_names_and_paths['magic_animate'][0], folder_paths.supported_pt_extensions) # | {'.json'})

magic_animate_checkpoints = folder_paths.get_filename_list("magic_animate")

assert len(magic_animate_checkpoints) > 0, "ERROR: No Magic Animate checkpoints found. Please download & place them in the ComfyUI/models/magic_animate folder, and restart ComfyUI."

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
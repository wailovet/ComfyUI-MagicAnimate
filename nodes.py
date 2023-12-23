from torchvision.transforms import ToTensor, ToPILImage
from einops import rearrange, repeat
import gc
import folder_paths
import torch
import os
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
import math
import numpy as np
from comfy.samplers import KSampler
from comfy.model_patcher import ModelPatcher
from comfy.sd import VAE

from comfy.sd import CLIP
from comfy.sd1_clip import SDTokenizer

from comfy.sd1_clip import SD1Tokenizer,SD1ClipModel,SDClipModel

from omegaconf import OmegaConf
from diffusers import AutoencoderKL, StableDiffusionPipeline
from diffusers import DDIMScheduler, UniPCMultistepScheduler,LCMScheduler, EulerDiscreteScheduler, EulerAncestralDiscreteScheduler
from transformers import CLIPTextModel, CLIPTokenizer
from magicanimate.models.unet_controlnet import UNet3DConditionModel
from magicanimate.models.controlnet import ControlNetModel
from magicanimate.models.appearance_encoder import AppearanceEncoderModel
from magicanimate.models.mutual_self_attention import ReferenceAttentionControl
from magicanimate.pipelines.pipeline_animation import AnimationPipeline
from magicanimate.utils.util import save_videos_grid
from magicanimate.utils.dist_tools import distributed_init
from accelerate.utils import set_seed
from collections import OrderedDict
from PIL import Image
import convert_diffusers_to_sd




class MagicAnimateModelLoader:
    def __init__(self):
        self.models = {}
        
    @classmethod
    def INPUT_TYPES(s):
        magic_animate_checkpoints = folder_paths.get_filename_list("magic_animate")

        devices = []
        if True: #torch.cuda.is_available():
            devices.append("cuda")
        devices.append("cpu")

        return {
            "required": {
                "model": ("MODEL",),
                "clip": ("CLIP", ),
                "vae": ("VAE", ), 
                "controlnet" : (magic_animate_checkpoints ,{
                    "default" : magic_animate_checkpoints[0]
                }),
                "appearance_encoder" : (magic_animate_checkpoints ,{
                    "default" : magic_animate_checkpoints[0]
                }),
                "motion_module" : (magic_animate_checkpoints ,{
                    "default" : magic_animate_checkpoints[0]
                }),
                "device" : (devices,),
            },
        }

    RETURN_TYPES = ("MAGIC_ANIMATE_MODEL",)

    FUNCTION = "load_model"

    CATEGORY = "ComfyUI Magic Animate"

    def load_model(self, model: ModelPatcher, clip:CLIP, vae:VAE, controlnet, appearance_encoder, motion_module, device): 
         
        if self.models:
            # delete old models
            all_keys = list(self.models.keys())
            for key in all_keys:
                # clear memory
                del self.models[key]
            self.models = {}
            gc.collect()
 
        current_dir = os.path.dirname(os.path.realpath(__file__))
        config  = OmegaConf.load(os.path.join(current_dir, "configs", "prompts", "animation.yaml"))
        inference_config = OmegaConf.load(os.path.join(current_dir, "configs", "inference", "inference.yaml"))
        magic_animate_models_dir = folder_paths.get_folder_paths("magic_animate")[0]
         
        
        config.pretrained_appearance_encoder_path = os.path.join(magic_animate_models_dir, os.path.dirname(appearance_encoder))
        config.pretrained_controlnet_path = os.path.join(magic_animate_models_dir, os.path.dirname(controlnet))
        motion_module = os.path.join(magic_animate_models_dir, motion_module)
        config.motion_module = motion_module

        ### >>> create animation pipeline >>> ###
        # tokenizer = CLIPTokenizer.from_pretrained(config.pretrained_model_path, subfolder="tokenizer")
        tokenizer:SD1Tokenizer = clip.tokenizer
        tokenizer:SDTokenizer = getattr(tokenizer, tokenizer.clip)
        tokenizer:CLIPTokenizer = tokenizer.tokenizer 


        # text_encoder = CLIPTextModel.from_pretrained(config.pretrained_model_path, subfolder="text_encoder") 
        text_encoder = clip.get_sd()
        text_encoder = convert_diffusers_to_sd.clip_from_state_dict(text_encoder) 

        # if config.pretrained_unet_path:
        #     unet = UNet3DConditionModel.from_pretrained_2d(config.pretrained_unet_path, unet_additional_kwargs=OmegaConf.to_container(inference_config.unet_additional_kwargs))
        # else:
        #     unet = UNet3DConditionModel.from_pretrained_2d(config.pretrained_model_path, subfolder="unet", unet_additional_kwargs=OmegaConf.to_container(inference_config.unet_additional_kwargs))
        
      

        model_state_dict = model.model_state_dict()
        model_state_dict = convert_diffusers_to_sd.unet_convert(model_state_dict)
        unet = UNet3DConditionModel.from_state_dict(
            model_state_dict, unet_additional_kwargs=OmegaConf.to_container(inference_config.unet_additional_kwargs))
        


        appearance_encoder = AppearanceEncoderModel.from_pretrained(config.pretrained_appearance_encoder_path).to(device)
        reference_control_writer = ReferenceAttentionControl(appearance_encoder, do_classifier_free_guidance=True, mode='write', fusion_blocks=config.fusion_blocks)
        
        reference_control_reader = ReferenceAttentionControl(unet, do_classifier_free_guidance=True, mode='read', fusion_blocks=config.fusion_blocks)
        
        vae = convert_diffusers_to_sd.vae_from_state_dict(vae.get_sd())

        # vae = vae.first_stage_model

        ### Load controlnet
        controlnet = ControlNetModel.from_pretrained(config.pretrained_controlnet_path)

        # unet.enable_xformers_memory_efficient_attention()
        # appearance_encoder.enable_xformers_memory_efficient_attention()
        # controlnet.enable_xformers_memory_efficient_attention()

        vae.to(torch.float16)
        unet.to(torch.float16)
        text_encoder.to(torch.float16)
        appearance_encoder.to(torch.float16)
        controlnet.to(torch.float16)
 
 

        self.models['vae'] = vae
        self.models['text_encoder'] = text_encoder
        self.models['appearance_encoder'] = appearance_encoder
        self.models['tokenizer'] = tokenizer
        self.models['unet'] = unet
        self.models['controlnet'] = controlnet 
        self.models['config'] = config
        self.models['motion_module'] = motion_module
        self.models['device'] = device
        self.models['reference_control_writer'] = reference_control_writer
        self.models['reference_control_reader'] = reference_control_reader
        self.models['noise_scheduler_kwargs'] = inference_config.noise_scheduler_kwargs

        return (self.models,)



def load_animation_pipeline(models):
    vae = models['vae']
    text_encoder = models['text_encoder']
    tokenizer = models['tokenizer']
    unet = models['unet']
    controlnet = models['controlnet'] 
    device = models['device']

    sampler_name = models['sampler_name']
    print("sampler_name:",sampler_name)

    noise_scheduler_kwargs = models['noise_scheduler_kwargs']

    scheduler = None
    if sampler_name == "DDIM":
        scheduler = DDIMScheduler(**OmegaConf.to_container(noise_scheduler_kwargs))
    elif sampler_name == "UniPCMultistep":
        scheduler = UniPCMultistepScheduler(**OmegaConf.to_container(noise_scheduler_kwargs))
    elif sampler_name == "LCM":
        scheduler = LCMScheduler(**OmegaConf.to_container(noise_scheduler_kwargs))
    elif sampler_name == "EulerDiscrete":
        scheduler = EulerDiscreteScheduler(**OmegaConf.to_container(noise_scheduler_kwargs))
    elif sampler_name == "EulerAncestralDiscrete":
        scheduler = EulerAncestralDiscreteScheduler(**OmegaConf.to_container(noise_scheduler_kwargs))

    pipeline = AnimationPipeline(
        vae=vae, 
        text_encoder=text_encoder, 
        tokenizer=tokenizer, 
        unet=unet, 
        controlnet=controlnet,
        scheduler=scheduler,
    )
    motion_module_state_dict = torch.load(models['motion_module'], map_location="cpu")
    motion_module_state_dict = motion_module_state_dict['state_dict'] if 'state_dict' in motion_module_state_dict else motion_module_state_dict
    try:
        # extra steps for self-trained models
        state_dict = OrderedDict()
        for key in motion_module_state_dict.keys():
            if key.startswith("module."):
                _key = key.split("module.")[-1]
                state_dict[_key] = motion_module_state_dict[key]
            else:
                state_dict[key] = motion_module_state_dict[key]
        motion_module_state_dict = state_dict
        del state_dict
        missing, unexpected = pipeline.unet.load_state_dict(motion_module_state_dict, strict=False)
        assert len(unexpected) == 0
    except:
        _tmp_ = OrderedDict()
        for key in motion_module_state_dict.keys():
            if "motion_modules" in key:
                if key.startswith("unet."):
                    _key = key.split('unet.')[-1]
                    _tmp_[_key] = motion_module_state_dict[key]
                else:
                    _tmp_[key] = motion_module_state_dict[key]
        missing, unexpected = unet.load_state_dict(_tmp_, strict=False)
        assert len(unexpected) == 0
        del _tmp_
    del motion_module_state_dict

    
    pipeline.to(device)

    return pipeline

class MagicAnimate:
    def __init__(self):
        self.generator = torch.Generator(device=torch.device("cuda:0"))
        
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "magic_animate_model": ("MAGIC_ANIMATE_MODEL",),
                "image" : ("IMAGE",),
                "pose_video" : ("IMAGE",),
                "seed" : ("INT", {
                    "display": "number" # Cosmetic only: display as "number" or "slider"
                }),
                "inference_steps" : ("INT", {
                    "default" : 25,
                    "display": "number" # Cosmetic only: display as "number" or "slider"
                }),
                "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step":0.1, "round": 0.01}),
                "sampler_name": (["DDIM", "UniPCMultistep", "LCM", "EulerDiscrete", "EulerAncestralDiscrete"],),
                # "scheduler": (KSampler.SCHEDULERS, ),
            },
            "optional": {
                "prompt": ("STRING",{
                    "multiline": True,
                    "default": "(masterpiece)"
                }),
                "negative_prompt": ("STRING",{
                    "multiline": True,
                    "default": "(blurry, low resolution, low quality, low fidelity, low res, low def, low-def, low-res)"
                }),
            },
        }

    RETURN_TYPES = ("IMAGE",) #() #("IMAGE",)
    # OUTPUT_NODE = True

    FUNCTION = "generate"

    CATEGORY = "ComfyUI Magic Animate"

    def resize_image_frame(self, image_tensor, size):
        # if image_tensor is a numpy array, convert it to a tensor
        if isinstance(image_tensor, np.ndarray):
            image_tensor = torch.from_numpy(image_tensor)
        # permute to C x H x W
        image_tensor = rearrange(image_tensor, 'h w c -> c h w')
        # print(image.shape)
        image_tensor = ToPILImage()(image_tensor)
        # print(image_tensor.shape)
        image_tensor = image_tensor.resize((size, size))
        # print(image_tensor.shape)
        image_tensor = ToTensor()(image_tensor)
        # permute back to H x W x C
        image_tensor = rearrange(image_tensor, 'c h w -> h w c')
        return image_tensor

    def resize_image_frame_wh(self, image_tensor, size_w, size_h):
        # if image_tensor is a numpy array, convert it to a tensor
        if isinstance(image_tensor, np.ndarray):
            image_tensor = torch.from_numpy(image_tensor)
        # permute to C x H x W
        image_tensor = rearrange(image_tensor, 'h w c -> c h w')
        # print(image.shape)
        image_tensor = ToPILImage()(image_tensor)
        # print(image_tensor.shape)
        image_tensor = image_tensor.resize((size_w, size_h))
        # print(image_tensor.shape)
        image_tensor = ToTensor()(image_tensor)
        # permute back to H x W x C
        image_tensor = rearrange(image_tensor, 'c h w -> h w c')
        return image_tensor


    def generate(self, magic_animate_model, image, pose_video, seed, inference_steps, cfg, sampler_name, prompt, negative_prompt):
        num_actual_inference_steps = inference_steps 
 
        magic_animate_model['sampler_name'] = sampler_name
        pipeline = load_animation_pipeline(magic_animate_model)


        config = magic_animate_model['config']
        # size = config.size
        control = pose_video.detach().cpu().numpy() # (num_frames, H, W, C)
        size_w = control.shape[2] // 8 * 8
        size_h = control.shape[1] // 8 * 8


        appearance_encoder = magic_animate_model['appearance_encoder']
        reference_control_writer = magic_animate_model['reference_control_writer']
        reference_control_reader = magic_animate_model['reference_control_reader']

        assert image.shape[0] == 1, "Only one image input is supported"
        image = image[0]
        H, W, C = image.shape

        if H != size_h or W != size_w:
            # resize image to be (size, size)
            image = self.resize_image_frame_wh(image, size_w, size_h)
            # print(image.shape)
            H, W, C = image.shape
            
        image = image * 255  
        if control.shape[1] != size_h or control.shape[2] != size_w:
            # resize each frame in control to be (size, size)
            control = torch.stack([self.resize_image_frame_wh(frame, size_w, size_h) for frame in control], dim=0)
            control = control.detach().cpu().numpy()

        # print("control shape:", control.shape,size_w,size_h)
        init_latents = None

        original_length = control.shape[0]
        if control.shape[0] % config.L > 0:
            control = np.pad(control, ((0, config.L-control.shape[0] % config.L), (0, 0), (0, 0), (0, 0)), mode='edge')
        control = control * 255
        self.generator.manual_seed(seed)

        dist_kwargs = {"rank":0, "world_size":1, "dist":False}

        sample = pipeline(
            prompt,
            negative_prompt         = negative_prompt,
            num_inference_steps     = inference_steps,
            guidance_scale          = cfg,
            width                   = W,
            height                  = H,
            video_length            = len(control),
            controlnet_condition    = control,
            init_latents            = init_latents,
            generator               = self.generator,
            num_actual_inference_steps = num_actual_inference_steps,
            appearance_encoder       = appearance_encoder, 
            reference_control_writer = reference_control_writer,
            reference_control_reader = reference_control_reader,
            source_image             = image.detach().cpu().numpy(),
            sampler_name            = sampler_name,
            **dist_kwargs,
        ).videos

        sample = sample[0, :, :original_length] # shape: (C, num_frames, H, W)

        # permute to (num_frames, H, W, C)
        sample = rearrange(sample, 'c f h w -> f h w c').detach().cpu()

        return (sample,)

# A dictionary that contains all nodes you want to export with their names
NODE_CLASS_MAPPINGS = {
    "MagicAnimateModelLoader" : MagicAnimateModelLoader,
    "MagicAnimate" : MagicAnimate,
}

# A dictionary that contains the friendly/humanly readable titles for the nodes
NODE_DISPLAY_NAME_MAPPINGS = {
    "MagicAnimateModelLoader" : "Load Magic Animate Model",
    "MagicAnimate" : "Magic Animate",
}
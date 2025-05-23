import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from tqdm import tqdm
from ddpm import DDPMSampler

WIDTH = 512
HEIGHT = 512
LATENT_WIDTH = WIDTH // 8
LATENT_HEIGHT = HEIGHT // 8

def generate(prompt: str, uncond_prompt: str, do_cfg = True, cfg_scale = 7.5, sampler_name = "ddpm", models = {}, n_inference_steps = 50, seed = None,
            device = None, idle_device=None, tokenizer = None, input_image = None, strength = 0.8, delta = None):
    """uncond_prompt: un prompt de ce qu'on ne veut pas voir dans l'image
        do_cfg: si on veut utiliser le classifier free guidance (CFG)
        cfg_scale: le coefficient de CFG, plus il est grand, plus le modèle va essayer de coller au prompt
        sampler_name: le nom du sampler à utiliser, par défaut "ddpm" (Denoising Diffusion Probabilistic Model)
        models: les modèles à utiliser pour la génération
        n_inference_steps: le nombre d'étapes de diffusion, plus il y en a, plus l'image est belle mais plus c'est long
        device: le device sur lequel on veut faire le calcul
        idle_device: le device sur lequel on veut faire le calcul si on est en mode idle (CPU)
        input_image: une image d'entrée à utiliser pour la diffusion image à image
        strength: la force de la diffusion image à image, plus elle est grande, plus l'image d'entrée va être modifiée
        delta: toutes les combien d'itération on veut sauvegarder une image pour montrer la progression
    """
    lstLatent = []

    with torch.no_grad():
        
        to_idle = lambda x: x
        if idle_device:
            to_idle = lambda x: x.to(idle_device)
        
        generator = torch.Generator(device=device) #generateur de nombres aléatoires pour la génération de bruit
        if seed: 
            generator.manual_seed(seed)
        else:
            generator.seed()

        clip = models["clip"]
        clip.to(device)

        ##CFG
        """
        La CFG (Classifier Free Guidance) est une technique utilisée pour améliorer la qualité des images générées
        Pour cela, on génère deux images : une avec le prompt et une sans le prompt
        l'output final est une combinaison des deux images: output = output_prompt + cfg_scale * (output_prompt - output_no_prompt)
        """
        if do_cfg:
            # On encode le prompt et le uncond_prompt en tokens
            cond_tokens = tokenizer.batch_encode_plus([prompt], padding = "max_length", max_length = 77).input_ids
            uncond_tokens = tokenizer.batch_encode_plus([uncond_prompt], padding = "max_length", max_length = 77).input_ids

            #B, seq_len
            cond_tokens = torch.tensor(cond_tokens, dtype = torch.long, device = device)
            uncond_tokens = torch.tensor(uncond_tokens, dtype = torch.long, device = device)

            #Embedding des tokens
            #B, seq_len -> B, seq_len, dim
            cond_context = clip(cond_tokens)
            uncond_context = clip(uncond_tokens)

            #On concatène les deux contextes pour avoir un seul contexte
            #2, seq_len, dim -> 2, 77, 768
            context = torch.cat([cond_context, uncond_context])

        else:
            # sans cfg, output = output_prompt
            tokens = tokenizer.batch_encode_plus([prompt], padding = "max_length", max_length = 77).input_ids
            tokens = torch.tensor(tokens, dtype = torch.long, device = device)
            #1, 77, 768
            context = clip(tokens)
        
        to_idle(clip) # On remet le clip sur le CPU pour libérer de la mémoire GPU

        if sampler_name == "ddpm":
            sampler = DDPMSampler(generator)
            sampler.set_inference_timesteps(n_inference_steps)
        
        else:
            raise ValueError(f"Sampler {sampler_name} not found")
        
        latent_shape = (1, 4, LATENT_HEIGHT, LATENT_WIDTH) #B, C, H, W

        #On traite le cas de la diffusion image à image
        if input_image:
            #On prépare l'image d'entrée: convertir en un tenseur, la passer au VAE, ajouter le bruit
            encoder = models["encoder"]
            encoder.to(device)

            input_image_tensor = input_image.resize((WIDTH, HEIGHT))
            input_image_tensor = np.array(input_image_tensor)
            #H, W, C (C = channels, 3 pour une image RGB)
            input_image_tensor = torch.tensor(input_image_tensor, dtype = torch.float32, device = device)

            #On rescale l'image pour donner aux pixels des valeurs entre -1 et 1
            input_image_tensor = rescale(input_image_tensor, (0, 255), (-1, 1))

            #H, W, C -> B, H, W, C
            input_image_tensor = input_image_tensor.unsqueeze(0)

            #B, H, W, C -> B, C, H, W
            input_image_tensor = input_image_tensor.permute(0, 3, 1, 2)

            encoder_noise = torch.randn(latent_shape, generator = generator, device = device)

            #On passe l'image au VAE pour l'encoder
            latent = encoder(input_image_tensor, encoder_noise)

            #Maintenant on va rajouter du bruit à l'image encodée
            #On rajoute + ou - de bruit selon strength
            #+ on ajoute de bruit, + l'image sera modifiée 
            sampler.set_strength(strength = strength)
            latent = sampler.add_noise(latent, sampler.timesteps[0]) 

            to_idle(encoder) # On remet le VAE sur le CPU pour libérer de la mémoire GPU

        else:
            #si on n'a pas d'image d'entrée, on commence avec du bruit aléatoire
            latent = torch.randn(latent_shape, generator = generator, device = device) #On génère du bruit aléatoire

        diffusion = models["diffusion"]
        diffusion.to(device)

        timesteps = tqdm(sampler.timesteps)
        for i, t in enumerate(timesteps):
            t = int(t)
            #scalaire -> (1, 320)
            time_embedding = get_time_embedding(t).to(device) 

            #B, C = 4, H, W
            model_input = latent

            if do_cfg:
                #B, 4, H, W -> 2*B, 4, H, W
                model_input = model_input.repeat(2, 1, 1, 1) #On répète l'image pour avoir deux images: une avec le prompt et une sans le prompt

            model_output = diffusion(model_input, context, time_embedding) #bruit prédit par le UNET

            if do_cfg:
                #On sépare les deux images: une avec le prompt et une sans le prompt
                output_cond, output_uncond = model_output.chunk(2)

                #On applique la CFG
                model_output = output_uncond + cfg_scale * (output_cond - output_uncond)

            #On enlève le bruit prédit de l'image
            latent = sampler.step(t, latent, model_output)

            if delta is not None:
                if i % delta == 0:
                    lstLatent.append(latent)
        
        lstLatent.append(latent)

        to_idle(diffusion) # On remet le UNET sur le CPU pour libérer de la mémoire GPU

        decoder = models["decoder"].to(device)
        lstImages = []
        for i,latent in enumerate(lstLatent):
            #On décode l'image latente pour obtenir l'image finale
            image = decoder(latent)
            
            #On rescale l'image pour donner aux pixels des valeurs entre 0 et 255
            image = rescale(image, (-1, 1), (0, 255), clamp = True)
            
            #B, C, H, W -> B, H, W, C
            image = image.permute(0, 2, 3, 1)

            image = image.to("cpu", torch.uint8).numpy()
            lstImages.append(image[0]) #On retourne la première image de la batch (B = 1)

        to_idle(decoder)

        return lstImages

def rescale(tensor, old_range, new_range, clamp = False):
    """Rescale un tensor de old_range à new_range"""
    old_min, old_max = old_range
    new_min, new_max = new_range

    #On calcule le facteur de mise à l'échelle
    scale = (new_max - new_min) / (old_max - old_min)

    #On applique la mise à l'échelle
    tensor = (tensor - old_min) * scale + new_min 

    if clamp:
        tensor = torch.clamp(tensor, new_min, new_max)

    return tensor

def get_time_embedding(timestep: int):
    """transforme un timestep en son embedding
        on fait pour ca le meme positional encoding que pour les transformers: sin(pos/10000^(2*i/dim)) 
    """
    #(160)
    freqs = torch.pow(10000, -torch.arange(start = 0, end = 160, dtype=torch.float32) / 160)
    #(1, 160)
    x = torch.tensor([timestep], dtype = torch.float32)[:, None] * freqs[None]
    #(1, 160) -> (1, 320)
    return torch.cat([torch.cos(x), torch.sin(x)], dim = -1) #On concatène les sinus et cosinus pour avoir un seul embedding
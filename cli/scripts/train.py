# Adapted from https://github.com/huggingface/peft/blob/main/examples/sft/train.py

import os

os.environ["WANDB_PROJECT"] = "<my-amazing-project>"  # name your W&B project
os.environ["WANDB_LOG_MODEL"] = "checkpoint"  # log all model checkpoints


import sys
from typing import Optional
from dataclasses import dataclass, field
from accelerate import Accelerator

from transformers import HfArgumentParser, set_seed
from trl import SFTTrainer, SFTConfig

import torch
from datasets import load_dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
)

from peft import LoraConfig


import logging
import warnings

# Set the global logging level to ERROR to suppress warnings
logger = logging.getLogger("transformers.tokenization_utils_base")
logger.setLevel(logging.ERROR)
warnings.filterwarnings(
    "ignore",
    message="Upcasted low precision parameters in Linear because mixed precision turned on in FSDP.*",
)
warnings.filterwarnings(
    "ignore",
    message="FSDP upcast of low precision parameters may affect the precision of model checkpoints.*",
)


# Define and parse arguments.
@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """

    model_name_or_path: str = field(
        metadata={
            "help": "Path to pretrained model or model identifier from huggingface.co/models"
        }
    )
    chat_template_format: Optional[str] = field(
        default="none",
        metadata={
            "help": "chatml|zephyr|none. Pass `none` if the dataset is already formatted with the chat template."
        },
    )
    lora_alpha: Optional[int] = field(default=16)
    lora_dropout: Optional[float] = field(default=0.1)
    lora_r: Optional[int] = field(default=64)
    lora_target_modules: Optional[str] = field(
        default="q_proj,k_proj,v_proj,o_proj,down_proj,up_proj,gate_proj",
        metadata={
            "help": "comma separated list of target modules to apply LoRA layers to"
        },
    )
    use_nested_quant: Optional[bool] = field(
        default=False,
        metadata={"help": "Activate nested quantization for 4bit base models"},
    )
    bnb_4bit_compute_dtype: Optional[str] = field(
        default="float16",
        metadata={"help": "Compute dtype for 4bit base models"},
    )
    bnb_4bit_quant_storage_dtype: Optional[str] = field(
        default="uint8",
        metadata={"help": "Quantization storage dtype for 4bit base models"},
    )
    bnb_4bit_quant_type: Optional[str] = field(
        default="nf4",
        metadata={"help": "Quantization type fp4 or nf4"},
    )
    use_flash_attn: Optional[bool] = field(
        default=False,
        metadata={"help": "Enables Flash attention for training."},
    )
    use_peft_lora: Optional[bool] = field(
        default=False,
        metadata={"help": "Enables PEFT LoRA for training."},
    )
    use_8bit_quantization: Optional[bool] = field(
        default=False,
        metadata={"help": "Enables loading model in 8bit."},
    )
    use_4bit_quantization: Optional[bool] = field(
        default=False,
        metadata={"help": "Enables loading model in 4bit."},
    )
    use_reentrant: Optional[bool] = field(
        default=False,
        metadata={"help": "Gradient Checkpointing param. Refer the related docs"},
    )
    use_unsloth: Optional[bool] = field(
        default=False,
        metadata={"help": "Enables UnSloth for training."},
    )


@dataclass
class DataTrainingArguments:
    train_filename: str = field(metadata={"help": "Path to the training data."})
    valid_filename: Optional[str] = field(
        default=None, metadata={"help": "Path to the validation data."}
    )
    # packing: Optional[bool] = field(
    #     default=False,
    #     metadata={"help": "Use packing dataset creating."},
    # )
    # max_seq_length: Optional[int] = field(default=512)
    # append_concat_token: Optional[bool] = field(
    #     default=False,
    #     metadata={"help": "If True, appends `eos_token_id` at the end of each sample being packed."},
    # )
    # add_special_tokens: Optional[bool] = field(
    #     default=False,
    #     metadata={"help": "If True, tokenizers adds special tokens to each sample being packed."},
    # )
    # splits: Optional[str] = field(
    #     default="train,test",
    #     metadata={"help": "Comma separate list of the splits to use from the dataset."},
    # )


def get_datasets(data_args):
    data_files = {"train": data_args.train_filename}
    if data_args.valid_filename is not None:
        if data_args.valid_filename != "-1":
            data_files["validation"] = data_args.valid_filename
    raw_dataset = load_dataset("json", data_files=data_files)
    return raw_dataset["train"], raw_dataset.get("validation", None)


def apply_chat_template(dataset, tokenizer):
    if dataset is None:
        return None

    def preprocess(example):
        return {
            "text": tokenizer.apply_chat_template(example["messages"], tokenize=False)
        }

    return dataset.map(preprocess)


def create_and_prepare_model(args, data_args, training_args):
    bnb_config = None

    if args.use_4bit_quantization:
        compute_dtype = getattr(torch, args.bnb_4bit_compute_dtype)

        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_storage=torch.bfloat16,
        )

        if compute_dtype == torch.float16 and args.use_4bit_quantization:
            major, _ = torch.cuda.get_device_capability()
            if major >= 8:
                print("=" * 80)
                print(
                    "Your GPU supports bfloat16, you can accelerate training with the argument --bf16"
                )
                print("=" * 80)
        elif args.use_8bit_quantization:
            bnb_config = BitsAndBytesConfig(load_in_8bit=args.use_8bit_quantization)

    model = AutoModelForCausalLM.from_pretrained(
        args.model_name_or_path,
        quantization_config=bnb_config,
        trust_remote_code=True,
        attn_implementation="flash_attention_2" if args.use_flash_attn else "eager",
        torch_dtype=torch.bfloat16,  # None if args.use_flash_attn else torch_dtype,
    )

    peft_config = LoraConfig(
        base_model_name_or_path=args.model_name_or_path,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        r=args.lora_r,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=args.lora_target_modules.split(",")
        if args.lora_target_modules != "all-linear"
        else args.lora_target_modules,
    )

    tokenizer = AutoTokenizer.from_pretrained(
        args.model_name_or_path, trust_remote_code=True
    )
    tokenizer.pad_token = tokenizer.eos_token

    return model, peft_config, tokenizer


def main(model_args, data_args, training_args):
    # Set seed for reproducibility
    set_seed(training_args.seed)

    # model
    model, peft_config, tokenizer = create_and_prepare_model(
        model_args, data_args, training_args
    )

    # gradient ckpt
    model.config.use_cache = not training_args.gradient_checkpointing
    training_args.gradient_checkpointing = (
        training_args.gradient_checkpointing and not model_args.use_unsloth
    )
    if training_args.gradient_checkpointing:
        training_args.gradient_checkpointing_kwargs = {
            "use_reentrant": model_args.use_reentrant
        }

    # datasets
    accelerator = Accelerator()
    with accelerator.main_process_first():
        train_dataset, eval_dataset = get_datasets(data_args)
        train_dataset = apply_chat_template(train_dataset, tokenizer)
        eval_dataset = apply_chat_template(eval_dataset, tokenizer)

    # trainer
    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset.select(range(100)),
        peft_config=peft_config,
    )

    # train
    trainer.train()

    # saving final model
    if trainer.is_fsdp_enabled:
        trainer.accelerator.state.fsdp_plugin.set_state_dict_type("FULL_STATE_DICT")
    trainer.save_model()


if __name__ == "__main__":
    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, SFTConfig))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args = parser.parse_json_file(
            json_file=os.path.abspath(sys.argv[1])
        )
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()
    main(model_args, data_args, training_args)

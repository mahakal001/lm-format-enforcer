from typing import List, Tuple, Union
from transformers import AutoModelForCausalLM

from .characterlevelparser import CharacterLevelParser
from transformers.generation.logits_process import LogitsWarper, PrefixConstrainedLogitsProcessor
from transformers.tokenization_utils import PreTrainedTokenizerBase

import torch

from .tokenenforcer import TokenEnforcer

class LogitsSaverWarper(LogitsWarper):
    def __init__(self) -> None:
        self.scores = []
        
    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor) -> torch.FloatTensor:
        cpu_scores = scores.clone().detach()
        self.scores.append(cpu_scores)
        return scores
    
class LogitsSaverManager:
    warper: LogitsSaverWarper

    def __init__(self, model: AutoModelForCausalLM):
        self.model = model
        self.warper = None
        self.old_warper = None

    def replace_logits_warper(self, filter_func = None):
        self.old_warper = self.model._get_logits_warper

        def new_logits_warper(generation_config):
            warpers = self.old_warper(generation_config)
            self.warper = LogitsSaverWarper()
            warpers.insert(0, self.warper)
            if filter_func is not None:
                processor = PrefixConstrainedLogitsProcessor(filter_func, 1)
                warpers.insert(1, processor)
            return warpers
        self.model._get_logits_warper = new_logits_warper

    def unreplace_logits_warper(self):
        self.model._get_logits_warper = self.old_warper

    def get_generated_scores(self, token_sequence):
        relevant_tokens = token_sequence[-len(self.warper.scores):]
        scores_matrix = torch.concat(self.warper.scores)  # n_tokens * vocab_size
        probs = torch.softmax(scores_matrix, dim=1)  # n_tokens * vocab_size
        token_probs = probs[torch.arange(probs.size(0)), relevant_tokens]  # n_tokens
        return token_probs.to('cpu').tolist()
    
    def get_leading_scores(self) -> Tuple[List[int], List[float]]:
        scores_matrix = torch.concat(self.warper.scores)  # n_tokens * vocab_size
        probs = torch.softmax(scores_matrix, dim=1)  # n_tokens * vocab_size
        best_tokens = torch.argmax(scores_matrix, dim=1)
        token_probs = probs[torch.arange(probs.size(0)), best_tokens]  # n_tokens
        token_probs_list = token_probs.to('cpu').tolist()
        return best_tokens.tolist(), token_probs_list


def _build_regular_tokens_list(tokenizer: PreTrainedTokenizerBase) -> List[Tuple[int, str]]:
    token_0 = tokenizer.encode("0")[-1]
    regular_tokens = []
    for token_idx in range(tokenizer.vocab_size):
        if token_idx in tokenizer.all_special_ids:
            continue
        # We prepend token 0 and skip the first letter of the result to get a space if the token is a start word.
        decoded = tokenizer.decode([token_0, token_idx])[1:]
        regular_tokens.append((token_idx, decoded))
    return regular_tokens


def generate_enforced(model: AutoModelForCausalLM, 
                      tokenizer: PreTrainedTokenizerBase, 
                      character_level_parser: CharacterLevelParser, 
                      **kwargs: dict) -> Union[str, dict]:
    
    regular_tokens = _build_regular_tokens_list(tokenizer)
    token_enforcer = TokenEnforcer(regular_tokens, character_level_parser, tokenizer.decode, tokenizer.eos_token_id)

    def transformers_filter_allowed_tokens(batch_id: int, sent: torch.Tensor) -> List[int]:
        token_sequence = sent.tolist()
        return token_enforcer.get_allowed_tokens(token_sequence)
    
    is_multi_inputs = kwargs['input_ids'].shape[0] > 1
    is_multi_beams = kwargs.get('num_beams', 1) > 1
    support_diagnostics = not (is_multi_inputs or is_multi_beams)  # TODO: Support diagnostics in these cases as well.
    return_dict_in_generate = kwargs.get('return_dict_in_generate', False)
    output_scores = kwargs.get('output_scores', None)

    # We do some internals hacking in order to extract the data needed for diagnostics. If we weren't asked for them,
    # we are better off simply using prefix_allowed_tokens_fn parameter.
    should_run_in_advanced_mode = return_dict_in_generate and output_scores and support_diagnostics

    if should_run_in_advanced_mode:
        logits_saver = LogitsSaverManager(model)
        logits_saver.replace_logits_warper(transformers_filter_allowed_tokens)
        generate_kwargs = kwargs
        
        try:
            output = model.generate(**generate_kwargs)
        finally:
            logits_saver.unreplace_logits_warper()

        sequence = output.sequences[0] if return_dict_in_generate else output[0]
        sequence = output.sequences[0]
        generated_scores = logits_saver.get_generated_scores(sequence)
        generated_tokens = sequence[-len(generated_scores):].to('cpu').tolist()
        single_token_strs = [tokenizer.convert_ids_to_tokens([token], skip_special_tokens=False)[0] for token in generated_tokens]

        leading_tokens, leading_scores = logits_saver.get_leading_scores()
        leading_token_strs = [tokenizer.convert_ids_to_tokens([token], skip_special_tokens=False)[0] for token in leading_tokens]
        df_dict = {}  # In order to minimize the package's dependencies, we don't create a dataframe, but create a dataframe-like dictionary instead.
        df_dict['generated_token'] = single_token_strs
        df_dict['generated_token_idx'] = generated_tokens
        df_dict['generated_score'] = generated_scores
        df_dict['leading_token'] = leading_token_strs
        df_dict['leading_token_idx'] = leading_tokens
        df_dict['leading_score'] = leading_scores
        output.enforced_scores = df_dict
    else:
        output = model.generate(**kwargs, prefix_allowed_tokens_fn=transformers_filter_allowed_tokens)
    
    return output
from typing import Dict, Optional

import abc
import os
import time

import openai
import numpy as np
import tiktoken

class Model(abc.ABC):
    """This class wraps the model API. It can take text and a logit bias and return text outputs."""
    
    @abc.abstractproperty
    def vocab_size() -> int:
        return -1

    @abc.abstractmethod
    def argmax(self, prefix: str, logit_bias: Dict[str, float] = {}) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    def topk(self, prefix: str, logit_bias: Dict[str, float] = {}) -> Dict[int, float]:
        raise NotImplementedError

    def median_topk(self, k, *args, **kwargs):
        """Runs the same topk query multiple times and returns the median. Useful
        to combat API nondeterminism when calling topk()."""
        results = [self.topk(*args, **kwargs) for _ in range(k)]
        return {
            word: np.median([result[word] for result in results])
            for word in results[0].keys()
        }
    def median_argmax(self, k, *args, **kwargs):
        """Runs the same argmax query multiple times and returns the median. Useful
        to combat API nondeterminism when calling argmax()."""
        results = [self.argmax(*args, **kwargs) for _ in range(k)]
        return np.median()


class OpenAIModel(Model):
    """Model wrapper for OpenAI API."""
    def __init__(self, model: str, system: Optional[str] = None):
        self.model = model
        self.encoding = tiktoken.encoding_for_model(model)
        self.client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self.system = (system or "You are a helpful assistant.")
        self.max_api_retries = 5
    
    @property
    def vocab_size(self) -> int:
        return self.encoding.n_vocab
    
    def argmax(self, prefix: str, logit_bias:  Dict[str, float] = {}) -> int:
        model = self.model
        system = self.system
        enc = tiktoken.encoding_for_model(model)
        if model == "gpt-3.5-turbo-instruct":
            if logit_bias is not None:
                response = self.client.completions.create(
                    model=model,
                    prompt=prefix,
                    temperature=0,
                    max_tokens=1,
                    logit_bias=logit_bias,
                    n=1,
                )
            else:
                response = self.client.completions.create(
                    model=model,
                    prompt=prefix,
                    temperature=0,
                    max_tokens=1,
                    n=1,
                )
            output = response.choices[0].text
            eos_idx = enc.encode(
                "<|endoftext|>", allowed_special={"<|endoftext|>", "<|im_start|>"}
            )[0]
            outputs = [choice.text for choice in response.choices]
        else:
            if logit_bias is not None:
                response = self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prefix},
                    ],
                    temperature=0,
                    max_tokens=1,
                    logit_bias=logit_bias,
                    n=1,
                )
            else:
                response = self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prefix},
                    ],
                    temperature=0,
                    max_tokens=1,
                    n=1,
                )
            output = response.choices[0].message["content"]
            outputs = [choice.message["content"] for choice in response.choices]
            eos_idx = enc.encode(
                "<|endoftext|>", allowed_special={"<|endoftext|>", "<|im_start|>"}
            )[0]

        return enc.encode(output)[0]
    
    def topk(self, prefix: str, logit_bias: Dict[str, float] = {}) -> Dict[int, float]:
        for i in range(self.max_api_retries):
            try:
                if self.model in ("gpt-3.5-turbo-instruct", "babbage-002", "davinci-002"):
                    return self._instruct_logprobs(prefix, logit_bias)
                elif self.model.startswith("gpt-4") or self.model.startswith("gpt-3"):
                    return self._chat_logprobs(prefix, logit_bias)
                else:
                    #this covers all openai text model names, shouldn't get here
                    raise NotImplementedError(f"Tried to get topk logprobs for: {self.model}")
                
            except IndexError: # response is empty so response.choices[0] is indexing empty list
                print(f"OpenAI API call fail; sleeping for {10*(i+1)} seconds")
                time.sleep(10 * (i+1))
    
    def _instruct_logprobs(self, prefix: str, logit_bias: Dict[int, float] = {}) -> Dict[int, float]:
        if logit_bias is not None:
            response = self.client.completions.create(
                model=self.model,
                prompt=prefix,
                temperature=1,
                max_tokens=1,
                logit_bias=logit_bias,
                logprobs=5,
            )
        else:
            response = self.client.completions.create(
                model=self.model,
                prompt=prefix,
                temperature=1,
                max_tokens=1,
                logprobs=5,
            )
        topk_dict = response.choices[0].logprobs.top_logprobs[0]
        return {self.encoding.encode(x)[0]: y for x, y in topk_dict.items()}
    
    def _chat_logprobs(self, prefix: str, logit_bias: Dict[int, float] = {}) -> Dict[int, float]:
        if logit_bias is not None:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system},
                    {"role": "user", "content": prefix},
                ],
                temperature=1,
                max_tokens=1,
                logit_bias=logit_bias,
                logprobs=True,
                top_logprobs=5
            )
        else:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system},
                    {"role": "user", "content": prefix},
                ],
                temperature=1,
                max_tokens=1,
                logprobs=True,
                top_logprobs=5
            )
        raw_logprobs = response.choices[0].logprobs.content[0].top_logprobs
        topk_dict = {self.encoding.encode(logprob.token)[0]: logprob.logprob for logprob in raw_logprobs}
        return topk_dict
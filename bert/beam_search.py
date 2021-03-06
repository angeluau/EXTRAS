import os
import time
import torch
from utils.batcher import Batcher
from utils.data import Vocab, VocabBert
from utils import data, config
from utils.utils import write_for_rouge, rouge_eval, rouge_log
import helper
import numpy as np
import pickle
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class Hypothesis:
	def __init__(self, tokens, log_probs):
		self.tokens = tokens
		self.log_probs = log_probs

	def extend(self, token, log_prob):
		return Hypothesis(tokens = self.tokens + [token],
					  log_probs = self.log_probs + [log_prob])
	@property
	def latest_token(self):
		return self.tokens[-1]

	@property
	def avg_log_prob(self):
		return sum(self.log_probs) / len(self.tokens)


class BeamSearchDecoder:

	def __init__(self, model):
		
		self._decode_dir = os.path.join(config.log_root, 'decode_%s' % ("model_bert_coverage"))
		self._rouge_ref_dir = os.path.join(self._decode_dir, 'rouge_ref')
		self._rouge_dec_dir = os.path.join(self._decode_dir, 'rouge_dec_dir')

		for p in [self._decode_dir, self._rouge_ref_dir, self._rouge_dec_dir]:
			if not os.path.exists(p):
				os.mkdir(p)

		self.vocab = VocabBert(config.vocab_path, config.vocab_size)
		self.batcher = Batcher(config.decode_data_path, self.vocab, mode='decode',
							   batch_size=config.beam_size, single_pass=True)
		self.model = model	

	def beam_search(self, batch, conf):
		#batch should have only one example
		enc_batch, enc_padding_mask, enc_lens, enc_batch_extend_vocab, extra_zeros = helper.prepare_src_batch(batch)
		encoder_output, _ = self.model.encoder.forward(enc_batch, enc_padding_mask.squeeze(1))

		hyps_list = [Hypothesis(tokens=[self.vocab.word2id(data.START_DECODING)], log_probs=[0.0]) for _ in range(config.beam_size)]
		results = []
		steps = 0
		yt = torch.zeros(config.beam_size, 1).long().to(device)
		while steps < config.max_dec_steps and len(results) < config.beam_size:
			latest_tokens = [h.latest_token for h in hyps_list]
			latest_tokens = [t if t < self.vocab.size() else self.vocab.word2id(data.UNKNOWN_TOKEN) for t in latest_tokens]

			curr_yt = torch.LongTensor(latest_tokens).unsqueeze(1).to(device) # [Bx1]
			yt = torch.cat((yt,curr_yt), dim=1)

			out, _ = self.model.decode(encoder_output, yt[:,1:], enc_padding_mask, helper.subsequent_mask(yt[:,1:].size(-1)))
			extra_zeros_ip=None
			if extra_zeros is not None:
				extra_zeros_ip = extra_zeros[:, 0:steps+1, :]

			if config.coverage:
				op_dist, _ = self.model.generator(out, encoder_output, enc_padding_mask, enc_batch_extend_vocab, extra_zeros_ip) 
			else:
				op_dist = self.model.generator(out, encoder_output, enc_padding_mask, enc_batch_extend_vocab, extra_zeros_ip) 

			log_probs = op_dist[:,-1,:]
			topk_log_probs, topk_ids = torch.topk(log_probs, config.beam_size*2)

			all_hyps = []
			num_orig_hyps = 1 if steps == 0 else len(hyps_list)

			for i in range(num_orig_hyps):
				h = hyps_list[i]

				for j in range(config.beam_size*2):  # for each of the top beam_size hyps:
					hyp = h.extend(token=topk_ids[i, j].item(), log_prob=topk_log_probs[i, j].item())
					all_hyps.append(hyp)

			hyps_list = []
			sorted_hyps = sorted(all_hyps, key=lambda h: h.avg_log_prob, reverse=True)
			for h in sorted_hyps:
				if h.latest_token == self.vocab.word2id(data.STOP_DECODING):
					if steps >= config.min_dec_steps:
						results.append(h)
				else:
					hyps_list.append(h)
				if len(hyps_list) == config.beam_size or len(results) == config.beam_size:
					break

			steps += 1

		if len(results) == 0:
			results = hyps_list

		results_sorted = sorted(results, key=lambda h: h.avg_log_prob, reverse=True)
		return results_sorted[0]


	def decode(self, conf):

		self.model.eval()
		start = time.time()
		counter = 0
		batch = self.batcher.next_batch()
		article_list = list()
		i =0
		while batch is not None:

			i+=1
			# Run beam search to get best Hypothesis
			best_summary = self.beam_search(batch, conf)

			# Extract the output ids from the hypothesis and convert back to words
			output_ids = [int(t) for t in best_summary.tokens[1:]]
			# print(output_ids)
			decoded_words = data.outputids2words(output_ids, self.vocab,
												 (batch.art_oovs[0] if config.pointer_gen else None))

			# Remove the [STOP] token from decoded_words, if necessary
			try:
				fst_stop_idx = decoded_words.index(data.STOP_DECODING)
				decoded_words = decoded_words[:fst_stop_idx]

			except ValueError:
				decoded_words = decoded_words
			if i%100 == 0:
				print("Batch: {}".format(i))
				print(decoded_words)
			original_abstract_sents = batch.original_abstracts_sents[0]

			write_for_rouge(original_abstract_sents, decoded_words, counter,
							self._rouge_ref_dir, self._rouge_dec_dir)
			counter += 1
			if counter % 1000 == 0:
				print('%d example in %d sec'%(counter, time.time() - start))
				start = time.time()

			batch = self.batcher.next_batch()

		print("Decoder has finished reading dataset for single_pass.")
		print("Now starting ROUGE eval...")
		results_dict = rouge_eval(self._rouge_ref_dir, self._rouge_dec_dir)
		rouge_log(results_dict, self._decode_dir)



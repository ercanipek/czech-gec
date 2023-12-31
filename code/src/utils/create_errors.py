import string
import aspell
import errant
import random
import argparse
import numpy as np

# from edit import Edit
from .edit import Edit
from typing import List
from spacy.tokens import Doc
from itertools import compress
from abc import ABC, abstractmethod
from errant.annotator import Annotator

allowed_source_delete_tokens = [',', '.', '!', '?']
czech_diacritics_tuples = [('a', 'á'), ('c', 'č'), ('d', 'ď'), ('e', 'é', 'ě'), ('i', 'í'), ('n', 'ň'), ('o', 'ó'), ('r', 'ř'), ('s', 'š'),
                           ('t', 'ť'), ('u', 'ů', 'ú'), ('y', 'ý'), ('z', 'ž')]
czech_diacritizables_chars = [char for sublist in czech_diacritics_tuples for char in sublist] + [char.upper() for sublist in
                                                                                                  czech_diacritics_tuples for char in
                                                                                                  sublist]


class Error(ABC):
    def __init__(self, target_prob: float) -> None:
        self.target_prob = target_prob
        self.num_errors = 0
        self.num_possible_edits = 0

    @abstractmethod
    def __call__(self, parsed_sentence, annotator: Annotator, aspell_speller = None) -> List[Edit]:
        pass


class ErrorMeMne(Error):
    def __call__(self, parsed_sentence, annotator: Annotator, aspell_speller = None) -> List[Edit]:
        edits = []
        for i, token in enumerate(parsed_sentence):
            if token.text == "mně":
                c_toks = annotator.parse("mě")
                edit = Edit(token, c_toks, [i, i+1, i, i+1], type="MeMne")
                edits.append(edit)
            if token.text == "mě":
                c_toks = annotator.parse("mně")
                edit = Edit(token, c_toks, [i, i+1, i, i+1], type="MeMne")
                edits.append(edit)
        return edits


class ErrorReplace(Error):
    def __call__(self, parsed_sentence, annotator: Annotator, aspell_speller) -> List[Edit]:
        edits = []
        for i, token in enumerate(parsed_sentence):
            if token.text.isalpha():
                proposals = aspell_speller.suggest(token.text)[:10]
                if len(proposals) > 0:
                    new_token_text = np.random.choice(proposals)
                    c_toks = annotator.parse(new_token_text)
                    edit = Edit(token, c_toks, [i, i+1, i, i+1], type="Replace")
                    edits.append(edit)
        return edits


class ErrorInsert(Error):
    def __init__(self, target_prob: float, word_vocabulary) -> None:
        super().__init__(target_prob)
        self.word_vocabulary = word_vocabulary

    def __call__(self, parsed_sentence, annotator: Annotator, aspell_speller = None) -> List[Edit]:
        edits = []
        for i, token in enumerate(parsed_sentence):
            new_token_text = np.random.choice(self.word_vocabulary)
            c_toks = annotator.parse(new_token_text)
            edit = Edit(token, c_toks, [i, i, i, i+1], type="Insert")
            edits.append(edit)
        return edits


class ErrorDelete(Error):
    def __init__(self, target_prob: float) -> None:
        super().__init__(target_prob)
        self.allowed_source_delete_tokens = [',', '.', '!', '?']

    def __call__(self, parsed_sentence, annotator: Annotator, aspell_speller = None) -> List[Edit]:
        edits = []
        for i, token in enumerate(parsed_sentence):
            if token.text.isalpha() and token.text not in self.allowed_source_delete_tokens:
                c_toks = annotator.parse("")
                edit = Edit(token, c_toks, [i, i+1, i, i], type="Remove")
                edits.append(edit)
        return edits


class ErrorRecase(Error):
    def __call__(self, parsed_sentence, annotator: Annotator, aspell_speller = None) -> List[Edit]:
        edits = []
        for i, token in enumerate(parsed_sentence):
            if token.text.islower():
                new_token_text = token.text[0].upper() + token.text[1:]
            else:
                num_recase = min(len(token.text), max(1, int(np.round(np.random.normal(0.3, 0.4) * len(token.text)))))
                char_ids_to_recase = np.random.choice(len(token.text), num_recase, replace=False)
                new_token_text = ''
                for char_i, char in enumerate(token.text):
                    if char_i in char_ids_to_recase:
                        if char.isupper():
                            new_token_text += char.lower()
                        else:
                            new_token_text += char.upper()
                    else:
                        new_token_text += char
            c_toks = annotator.parse(new_token_text)
            edit = Edit(token, c_toks, [i, i+1, i, i+1], type="Recase")
            edits.append(edit)
        return edits


class ErrorSwap(Error):
    def __call__(self, parsed_sentence, annotator: Annotator, aspell_speller = None) -> List[Edit]:
        edits = []
        if len(parsed_sentence) > 1:
            previous_token = parsed_sentence[0]
            for i, token in enumerate(parsed_sentence[1:]):
                i = i + 1
                c_toks = annotator.parse(token.text + " " + previous_token.text)
                edit = Edit(token, c_toks, [i-1, i+1, i-1, i+1], type="Swap")
                edits.append(edit)
                previous_token = token
        return edits
    
class GeneralWordError(Error):
    def __init__(self, target_prob: float, word_vocabulary) -> None:
        super().__init__(target_prob)
        self.word_vocabulary = word_vocabulary

    def __call__(self, parsed_sentence, annotator: Annotator, aspell_speller = None) -> List[Edit]:
        # TODO: dodelat rychlejsi
        ...

# MAIN:
class ErrorGenerator:
    def __init__(self, word_vocabulary, char_vocabulary,
                 char_err_distribution, char_err_prob, char_err_std,
                 token_err_distribution, token_err_prob, token_err_std) -> None:
        self.char_err_distribution = char_err_distribution
        self.char_err_prob = char_err_prob
        self.char_err_std = char_err_std
        self.char_vocabulary = char_vocabulary

        self.token_err_distribution = token_err_distribution
        self.token_err_prob = token_err_prob
        self.token_err_std = token_err_std
        self.word_vocabulary = word_vocabulary

        self.annotator = None

        self.total_tokens = 0
        self.error_instances = [
            ErrorMeMne(
                0.01)
            # ErrorReplace(
            #     0.1050),
            # ErrorInsert(
            #     0.0150, word_vocabulary),
            # ErrorDelete(
            #     0.0075),
            # ErrorRecase(
            #     0.0150),
            # ErrorSwap(
            #     0.0075),
            # GeneralWordError(word_vocabulary)
        ]

    # def get_edits(self, parsed_sentence) -> List[Edit]:
    #     self.total_tokens += len(parsed_sentence)
    #     all_edits = []
    #     for error_instance in self.error_instances:
    #         edits = error_instance(parsed_sentence, self.annotator)
    #         ## rejection sampling
    #         selected_edits = []
    #         for edit in edits:
    #             gen_prob = error_instance.num_possible_edits / self.total_tokens if self.total_tokens > 0 else 0.5
    #             acceptance_prob = error_instance.target_prob / (gen_prob + 1e-10)
    #             if np.random.uniform(0, 1) < acceptance_prob:
    #                 selected_edits.append(edit)
    #                 error_instance.num_errors += 1
    #         error_instance.num_possible_edits += len(edits)
    #         ##
    #         all_edits = all_edits + selected_edits
    #     # TODO: Do not accept all edits.
    #     return all_edits

    def _init_annotator(self, lang: str = 'cs'):
        if self.annotator is None:
            self.annotator = errant.load(lang)

    def get_edits(self, parsed_sentence, annotator: Annotator, aspell_speller) -> List[Edit]:
        self.total_tokens += len(parsed_sentence)
        edits_errors = []
        for error_instance in self.error_instances:
            edits = error_instance(parsed_sentence, annotator, aspell_speller)
            edits_errors = edits_errors + [(edit, error_instance) for edit in edits]
        
        if len(edits_errors) == 0:
            return []

        # Overlaping:
        random.shuffle(edits_errors)
        mask = self.get_remove_mask(list(zip(*edits_errors))[0])
        edits_errors = list(compress(edits_errors, mask))
        
        ## Rejection Sampling:
        selected_edits = []
        for edit, error_instance in edits_errors:
            gen_prob = error_instance.num_possible_edits / self.total_tokens if self.total_tokens > 0 else 0.5
            acceptance_prob = error_instance.target_prob / (gen_prob + 1e-10)
            if np.random.uniform(0, 1) < acceptance_prob:
                selected_edits.append(edit)
                error_instance.num_errors += 1
        error_instance.num_possible_edits += len(edits_errors)
        ##

        # Sorting:
        sorted_edits = self.sort_edits(selected_edits)
        return sorted_edits
    
    def sort_edits(self, edits: List[Edit], reverse: bool = False) -> List[Edit]:
        reverse_index = -1 if reverse else 1
        minus_start_indices = [reverse_index * edit.o_end for edit in edits]
        sorted_edits = np.array(edits)
        sorted_edits = sorted_edits[np.argsort(minus_start_indices)]

        minus_start_indices = [reverse_index * edit.o_start for edit in sorted_edits]
        sorted_edits = np.array(sorted_edits)
        sorted_edits = sorted_edits[np.argsort(minus_start_indices)]

        return sorted_edits.tolist()


    def get_remove_mask(self, edits: List[Edit]) -> List[bool]:
        ranges = [(edit.o_start, edit.o_end) for edit in edits]
        removed = [not any([self.is_overlap(current_range, r) if j < i else False for j, r in enumerate(ranges)]) for i, current_range in enumerate(ranges)]
        # filtered_edits = list(compress(edits, removed))
        return removed

    def is_overlap(self, range_1: tuple, range_2: tuple) -> bool:
        start_1 = range_1[0]
        end_1 = range_1[1]
        start_2 = range_2[0]
        end_2 = range_2[1]

        if start_1 <= start_2:
            if end_1 > start_2:
                return True
        else:
            if end_2 > start_1:
                return True
        return False
    
    def get_m2_edits_text(self, sentence: str, annotator: Annotator, aspell_speller) -> List[str]:
        parsed_sentence = annotator.parse(sentence)
        edits = self.get_edits(parsed_sentence, annotator, aspell_speller)
        m2_edits = [edit.to_m2() for edit in edits]
        return m2_edits
    
    def introduce_token_level_errors_on_sentence(self, tokens, aspell_speller):
        num_errors = int(np.round(np.random.normal(self.token_err_prob, self.token_err_std) * len(tokens)))
        num_errors = min(max(0, num_errors), len(tokens))  # num_errors \in [0; len(tokens)]

        if num_errors == 0:
            return ' '.join(tokens)
        token_ids_to_modify = np.random.choice(len(tokens), num_errors, replace=False)

        new_sentence = ''
        for token_id in range(len(tokens)):
            if token_id not in token_ids_to_modify:
                if new_sentence:
                    new_sentence += ' '
                new_sentence += tokens[token_id]
                continue

            current_token = tokens[token_id]
            operation = np.random.choice(['replace', 'insert', 'delete', 'swap', 'recase'], p=self.token_err_distribution)
            new_token = ''
            if operation == 'replace':
                if not current_token.isalpha():
                    new_token = current_token
                else:
                    proposals = aspell_speller.suggest(current_token)[:10]
                    if len(proposals) > 0:
                        new_token = np.random.choice(proposals)  # [np.random.randint(0, len(proposals))]
                    else:
                        new_token = current_token
            elif operation == 'insert':
                new_token = current_token + ' ' + np.random.choice(self.word_vocabulary)
            elif operation == 'delete':
                if not current_token.isalpha() or current_token in allowed_source_delete_tokens:
                    new_token = current_token
                else:
                    new_token = ''
            elif operation == 'recase':
                if not current_token.isalpha():
                    new_token = current_token
                elif current_token.islower():
                    new_token = current_token[0].upper() + current_token[1:]
                else:
                    # either whole word is upper-case or mixed-case
                    if np.random.random() < 0.5:
                        new_token = current_token.lower()
                    else:
                        num_recase = min(len(current_token), max(1, int(np.round(np.random.normal(0.3, 0.4) * len(current_token)))))
                        char_ids_to_recase = np.random.choice(len(current_token), num_recase, replace=False)
                        new_token = ''
                        for char_i, char in enumerate(current_token):
                            if char_i in char_ids_to_recase:
                                if char.isupper():
                                    new_token += char.lower()
                                else:
                                    new_token += char.upper()
                            else:
                                new_token += char

            elif operation == 'swap':
                if token_id == len(tokens) - 1:
                    continue

                new_token = tokens[token_id + 1]
                tokens[token_id + 1] = tokens[token_id]

            if new_sentence and new_token:
                new_sentence += ' '
            new_sentence = new_sentence + new_token

        return new_sentence
    
    def introduce_char_level_errors_on_sentence(self, sentence):
        sentence = list(sentence)
        num_errors = int(np.round(np.random.normal(self.char_err_prob, self.char_err_std) * len(sentence)))
        num_errors = min(max(0, num_errors), len(sentence))  # num_errors \in [0; len(sentence)]
        if num_errors == 0:
            return ''.join(sentence)
        char_ids_to_modify = np.random.choice(len(sentence), num_errors, replace=False)
        new_sentence = ''
        for char_id in range(len(sentence)):
            if char_id not in char_ids_to_modify:
                new_sentence += sentence[char_id]
                continue
            operation = np.random.choice(['replace', 'insert', 'delete', 'swap', 'change_diacritics'], 1,
                                         p=self.char_err_distribution)
            current_char = sentence[char_id]
            new_char = ''
            if operation == 'replace':
                if current_char.isalpha():
                    new_char = np.random.choice(self.char_vocabulary)
                else:
                    new_char = current_char
            elif operation == 'insert':
                new_char = current_char + np.random.choice(self.char_vocabulary)
            elif operation == 'delete':
                if current_char.isalpha():
                    new_char = ''
                else:
                    new_char = current_char
            elif operation == 'swap':
                if char_id == len(sentence) - 1:
                    continue
                new_char = sentence[char_id + 1]
                sentence[char_id + 1] = sentence[char_id]
            elif operation == 'change_diacritics':
                if current_char in czech_diacritizables_chars:
                    is_lower = current_char.islower()
                    current_char = current_char.lower()
                    char_diacr_group = [group for group in czech_diacritics_tuples if current_char in group][0]
                    new_char = np.random.choice(char_diacr_group)
                    if not is_lower:
                        new_char = new_char.upper()
            new_sentence += new_char
        return new_sentence
    
    def create_error_sentence(self, sentence: str, aspell_speller, use_token_level: bool = False, use_char_level: bool = False) -> List[str]:
        parsed_sentence = self.annotator.parse(sentence)
        edits = self.get_edits(parsed_sentence, self.annotator, aspell_speller)
        
        edits = self.sort_edits(edits, reverse=True)

        sentence = self._use_edits(edits, parsed_sentence)
        
        if use_token_level:
            sentence = self.introduce_token_level_errors_on_sentence(sentence.split(' '), aspell_speller)

        if use_char_level:
            sentence = self.introduce_char_level_errors_on_sentence(sentence)

        return sentence
    
    def _use_edits(self, edits: List[Edit], parsed_sentence) -> str:
        if len(edits) == 0:
            return parsed_sentence.text
        docs = [edits[0].c_toks]
        prev_edit = edits[0]
        for edit in edits[1:]:
            next_docs = self._merge(prev_edit, edit, parsed_sentence)
            docs = next_docs + docs
            prev_edit = edit
        subtexts = [doc.text for doc in docs]
        sentence = parsed_sentence[:edits[-1].o_start].text + " " + " ".join(subtexts) + " " + parsed_sentence[edits[0].o_end:].text
        return sentence

    def _merge(self, prev_edit: Edit, next_edit: Edit, parsed_sentence) -> List:
        if prev_edit.o_start > next_edit.o_end:
            docs = [next_edit.c_toks, parsed_sentence[next_edit.o_end:prev_edit.o_start]]
        else:
            docs = [next_edit.c_toks]
        return docs

def get_token_vocabulary(tsv_token_file):
    tokens = []
    with open(tsv_token_file) as reader:
        for line in reader:
            line = line.strip('\n')
            token, freq = line.split('\t')
            if token.isalpha():
                tokens.append(token)
    return tokens

def get_char_vocabulary(lang):
    if lang == 'cs':
        czech_chars_with_diacritics = 'áčďěéíňóšřťůúýž'
        czech_chars_with_diacritics_upper = czech_chars_with_diacritics.upper()
        allowed_chars = ', .'
        allowed_chars += string.ascii_lowercase + string.ascii_uppercase + czech_chars_with_diacritics + czech_chars_with_diacritics_upper
        return list(allowed_chars)


def main(args):
    char_vocabulary = get_char_vocabulary(args.lang)
    word_vocabulary = get_token_vocabulary("../../data/vocabluraries/vocabulary_cs.tsv")
    aspell_speller = aspell.Speller('lang', args.lang)
    error_generator = ErrorGenerator(word_vocabulary, char_vocabulary,
                                     [0.2, 0.2, 0.2, 0.2, 0.2], 0.02, 0.01,
                                     [0.7, 0.1, 0.05, 0.1, 0.05], 0.15, 0.2)
    error_generator._init_annotator()
    input_path = args.input
    output_path = args.output
    with open(input_path, "r") as f:
        while True:
            line = f.readline()
            if not line:
                break
            line = line.strip()

            # m2_lines = error_generator.get_m2_edits_text(line, annotator, aspell_speller)
            # with open(output_path, "a+") as output_file:
            #     output_file.write("S " + line + "\n")
            #     for m2_line in m2_lines:
            #         output_file.write(m2_line + "\n")
            #     output_file.write("\n")

            error_line = error_generator.create_error_sentence(line, aspell_speller, True, True)
            with open(output_path, "a+") as output_file:
                output_file.write(error_line + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create m2 file with errors.")
    parser.add_argument('-i', '--input', type=str)
    parser.add_argument('-o', '--output', type=str, default="output.m2")
    parser.add_argument('-l', '--lang', type=str)

    args = parser.parse_args()
    main(args)

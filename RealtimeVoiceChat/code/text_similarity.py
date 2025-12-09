import re
from difflib import SequenceMatcher
import logging

logger = logging.getLogger(__name__)

class TextSimilarity:
    """
    Compares two text strings and calculates their similarity ratio.

    This class provides methods to calculate the similarity between two texts
    using `difflib.SequenceMatcher`. It supports different comparison strategies:
    comparing the full texts, focusing only on the last few words, or using a
    weighted average of both overall and end-focused similarity. Texts are
    normalized (lowercase, punctuation removed) before comparison.

    Attributes:
        similarity_threshold (float): The minimum similarity ratio (0.0 to 1.0)
                                      for texts to be considered similar by
                                      `are_texts_similar`.
        n_words (int): The number of words from the end of each text to consider
                       when using 'end' or 'weighted' focus modes.
        focus (str): The comparison strategy ('overall', 'end', or 'weighted').
        end_weight (float): The weight (0.0 to 1.0) assigned to the end-segment
                            similarity when `focus` is 'weighted'. The overall
                            similarity receives a weight of `1.0 - end_weight`.
    """
    def __init__(self,
                 similarity_threshold: float = 0.96,
                 n_words: int = 5,
                 focus: str = 'weighted', # Default to weighted approach
                 end_weight: float = 0.7): # Default: 70% weight on end similarity
        """
        Initializes the TextSimilarity comparator.

        Args:
            similarity_threshold: The ratio threshold for `are_texts_similar`.
                                  Must be between 0.0 and 1.0.
            n_words: The number of words to extract from the end for focused
                     comparison modes. Must be a positive integer.
            focus: The comparison strategy. Must be 'overall', 'end', or 'weighted'.
            end_weight: The weight for the end similarity in 'weighted' mode.
                        Must be between 0.0 and 1.0. Ignored otherwise.

        Raises:
            ValueError: If any argument is outside its valid range or type.
        """
        if not 0.0 <= similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be between 0.0 and 1.0")
        if not isinstance(n_words, int) or n_words < 1:
            raise ValueError("n_words must be a positive integer")
        if focus not in ['end', 'weighted', 'overall']:
            raise ValueError("focus must be 'end', 'weighted', or 'overall'")
        if not 0.0 <= end_weight <= 1.0:
            raise ValueError("end_weight must be between 0.0 and 1.0")

        self.similarity_threshold = similarity_threshold
        self.n_words = n_words
        self.focus = focus
        # Ensure end_weight is only relevant when focus is 'weighted'
        self.end_weight = end_weight if focus == 'weighted' else 0.0

        # Precompile regex for efficiency
        self._punctuation_regex = re.compile(r'[^\w\s]')
        self._whitespace_regex = re.compile(r'\s+')

    def _normalize_text(self, text: str) -> str:
        """
        Prepares text for comparison by simplifying it.

        Converts the input text to lowercase, removes all characters that are
        not alphanumeric or whitespace, collapses multiple whitespace characters
        into single spaces, and removes leading/trailing whitespace. Handles
        non-string inputs by logging a warning and returning an empty string.

        Args:
            text: The raw text string to normalize.

        Returns:
            The normalized text string. Returns an empty string if input is not
            a string or normalizes to empty.
        """
        if not isinstance(text, str):
             # Handle potential non-string inputs gracefully
             logger.warning(f"📏⚠️ Input is not a string: {type(text)}. Converting to empty string.")
             text = ""
        text = text.lower()
        text = self._punctuation_regex.sub('', text)
        text = self._whitespace_regex.sub(' ', text).strip()
        return text

    def _get_last_n_words_text(self, normalized_text: str) -> str:
        """
        Extracts the last `n_words` from a normalized text string.

        Splits the text by spaces and joins the last `n_words` back together.
        If the text has fewer than `n_words`, the entire text is returned.

        Args:
            normalized_text: A text string already processed by `_normalize_text`.

        Returns:
            A string containing the last `n_words` of the input, joined by spaces.
            Returns an empty string if the input is empty.
        """
        words = normalized_text.split()
        # Handles cases where text has fewer than n_words automatically
        last_words_segment = words[-self.n_words:]
        return ' '.join(last_words_segment)

    def calculate_similarity(self, text1: str, text2: str) -> float:
        """
        Calculates the similarity ratio between two texts based on the configuration.

        Normalizes both input texts, then calculates similarity using `difflib.SequenceMatcher`
        according to the `focus` strategy ('overall', 'end', or 'weighted').
        Handles empty strings appropriately after normalization.

        Args:
            text1: The first text string for comparison.
            text2: The second text string for comparison.

        Returns:
            A float between 0.0 and 1.0 representing the calculated similarity ratio.
            1.0 indicates identical sequences (after normalization and focusing),
            0.0 indicates no similarity.

        Raises:
            RuntimeError: If the instance's `focus` attribute has an invalid value
                          (should not happen due to __init__ validation).
        """
        norm_text1 = self._normalize_text(text1)
        norm_text2 = self._normalize_text(text2)

        # Handle edge case: Both normalized texts are empty -> perfect match
        if not norm_text1 and not norm_text2:
            return 1.0
        # Note: SequenceMatcher handles comparison with "" correctly (ratio 0.0 if other is non-empty),
        # so we don't need explicit checks for only one being empty.

        # Initialize matcher once, reuse it by setting sequences
        # autojunk=False forces detailed comparison, potentially slower but avoids heuristics.
        matcher = SequenceMatcher(isjunk=None, a=None, b=None, autojunk=False)

        if self.focus == 'overall':
            matcher.set_seqs(norm_text1, norm_text2)
            return matcher.ratio()

        elif self.focus == 'end':
            end_text1 = self._get_last_n_words_text(norm_text1)
            end_text2 = self._get_last_n_words_text(norm_text2)
            # SequenceMatcher handles empty strings correctly (("", "") -> 1.0, ("abc", "") -> 0.0)
            matcher.set_seqs(end_text1, end_text2)
            return matcher.ratio()

        elif self.focus == 'weighted':
            # Calculate overall similarity
            matcher.set_seqs(norm_text1, norm_text2)
            sim_overall = matcher.ratio()

            # Calculate end similarity
            end_text1 = self._get_last_n_words_text(norm_text1)
            end_text2 = self._get_last_n_words_text(norm_text2)

            # Reuse the matcher and let SequenceMatcher handle empty end segments
            # SequenceMatcher handles empty strings correctly (("", "") -> 1.0, ("abc", "") -> 0.0)
            matcher.set_seqs(end_text1, end_text2)
            sim_end = matcher.ratio()

            # Calculate weighted average
            weighted_sim = (1 - self.end_weight) * sim_overall + self.end_weight * sim_end
            return weighted_sim

        else:
            # This should not happen due to __init__ validation, but as a safeguard:
            # Adding icons for consistency, though this log indicates an internal error.
            # 💥 suggests an unexpected failure.
            logger.error(f"📏💥 Invalid focus mode encountered during calculation: {self.focus}")
            raise RuntimeError("Invalid focus mode encountered during calculation.")


    def are_texts_similar(self, text1: str, text2: str) -> bool:
        """
        Determines if two texts meet the similarity threshold.

        Calculates the similarity between `text1` and `text2` using the configured
        method (`calculate_similarity`) and compares the result against the
        instance's `similarity_threshold`.

        Args:
            text1: The first text string.
            text2: The second text string.

        Returns:
            True if the calculated similarity ratio is greater than or equal to
            `self.similarity_threshold`, False otherwise.
        """
        similarity = self.calculate_similarity(text1, text2)
        return similarity >= self.similarity_threshold

if __name__ == "__main__":
    # Configure basic logging for example output
    logging.basicConfig(level=logging.INFO)

    # --- Example Usage ---
    text_long1 = "This is a very long text that goes on and on, providing lots of context, but the important part is how it concludes in the final sentence."
    text_long2 = "This is a very long text that goes on and on, providing lots of context, but the important part is how it concludes in the final sentence."
    text_long_diff_end = "This is a very long text that goes on and on, providing lots of context, but the important part is how it finishes in the last words."
    text_short1 = "Check the end."
    text_short2 = "Check the ending."
    text_short_very = "End."
    text_empty = ""
    text_punct = "!!!"
    text_non_string = 12345

    print("--- Standard Similarity (Overall Focus) ---")
    sim_overall = TextSimilarity(focus='overall')
    print(f"Long Same: {sim_overall.calculate_similarity(text_long1, text_long2):.4f}") # Expected: 1.0000
    print(f"Long Diff End: {sim_overall.calculate_similarity(text_long1, text_long_diff_end):.4f}") # Expected: High (e.g., > 0.9)
    print(f"Short Diff End: {sim_overall.calculate_similarity(text_short1, text_short2):.4f}") # Expected: Moderate/High
    print(f"Short vs Very Short: {sim_overall.calculate_similarity(text_short1, text_short_very):.4f}") # Expected: Low/Moderate
    print(f"Empty vs Punct: {sim_overall.calculate_similarity(text_empty, text_punct):.4f}") # Expected: 1.0000 (both normalize to "")
    print(f"Short vs Empty: {sim_overall.calculate_similarity(text_short1, text_empty):.4f}") # Expected: 0.0000
    print(f"Non-string vs Empty: {sim_overall.calculate_similarity(text_non_string, text_empty):.4f}") # Expected: 1.0000 (both normalize to "")


    print("\n--- End Focus (Last 5 words) ---")
    sim_end_only = TextSimilarity(focus='end', n_words=5)
    print(f"Long Same: {sim_end_only.calculate_similarity(text_long1, text_long2):.4f}") # Expected: 1.0000
    print(f"Long Diff End: {sim_end_only.calculate_similarity(text_long1, text_long_diff_end):.4f}") # Expected: Lower (depends on last 5 words)
    print(f"Short Diff End: {sim_end_only.calculate_similarity(text_short1, text_short2):.4f}") # Compares 'check the end' vs 'check the ending' -> Moderate/High
    print(f"Short vs Very Short: {sim_end_only.calculate_similarity(text_short1, text_short_very):.4f}") # Compares 'check the end' vs 'end' -> Lower
    print(f"Empty vs Punct: {sim_end_only.calculate_similarity(text_empty, text_punct):.4f}") # Expected: 1.0000 (both normalize to "")
    print(f"Short vs Empty: {sim_end_only.calculate_similarity(text_short1, text_empty):.4f}") # Expected: 0.0000


    print("\n--- Weighted Focus (70% End, Last 5 words) ---")
    sim_weighted = TextSimilarity(focus='weighted', n_words=5, end_weight=0.7)
    print(f"Long Same: {sim_weighted.calculate_similarity(text_long1, text_long2):.4f}") # Expected: 1.0000
    print(f"Long Diff End: {sim_weighted.calculate_similarity(text_long1, text_long_diff_end):.4f}") # Expected: Lower than overall, higher than end-only
    print(f"Short Diff End: {sim_weighted.calculate_similarity(text_short1, text_short2):.4f}") # Expected: Weighted result
    print(f"Short vs Very Short: {sim_weighted.calculate_similarity(text_short1, text_short_very):.4f}") # Expected: Weighted result
    print(f"Empty vs Punct: {sim_weighted.calculate_similarity(text_empty, text_punct):.4f}") # Expected: 1.0000 (both normalize to "")
    print(f"Short vs Empty: {sim_weighted.calculate_similarity(text_short1, text_empty):.4f}") # Expected: 0.0000


    print("\n--- Handling Short Texts with End Focus (n_words=5) ---")
    short1 = "one two three"
    short2 = "one two four"
    short3 = "one"
    sim_end_short = TextSimilarity(focus='end', n_words=5)
    print(f"'{short1}' vs '{short2}': {sim_end_short.calculate_similarity(short1, short2):.4f}") # Compares full texts as < 5 words
    print(f"'{short1}' vs '{short3}': {sim_end_short.calculate_similarity(short1, short3):.4f}") # Compares full texts as < 5 words


    print("\n--- Threshold Check ---")
    threshold_checker = TextSimilarity(similarity_threshold=0.90, focus='overall')
    print(f"'{text_long1}' vs '{text_long_diff_end}' similar? {threshold_checker.are_texts_similar(text_long1, text_long_diff_end)}")
    print(f"'{text_short1}' vs '{text_short2}' similar? {threshold_checker.are_texts_similar(text_short1, text_short2)}")
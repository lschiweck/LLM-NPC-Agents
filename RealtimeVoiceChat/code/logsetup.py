import logging
import time
from colors import Colors # Assuming 'colors' library is installed (pip install ansicolors) or your custom Colors class
from typing import Optional # Added for type hint consistency if needed elsewhere, though not strictly used in current args/returns

# --- Define Custom Formatter to handle time locally ---
class CustomTimeFormatter(logging.Formatter):
    """
    A logging Formatter that displays timestamps as MM:SS.cs using local time.

    Inherits from `logging.Formatter` and overrides the `formatTime` method
    to provide a specific, concise timestamp format suitable for console output,
    using the local time zone derived from the log record's creation time.
    """

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        """
        Formats the log record's creation time into MM:SS.cs format.

        Uses `time.localtime` to convert the record's creation timestamp and
        formats it as minutes, seconds, and centiseconds. The `datefmt` argument
        provided by the base class is ignored in this custom implementation.

        Args:
            record: The log record whose creation time needs formatting.
            datefmt: An optional date format string (ignored by this method).

        Returns:
            A string representing the formatted time (e.g., "59:23.18").
        """
        # Use localtime as originally intended, but locally within this formatter
        now = time.localtime(record.created)
        cs = int((record.created % 1) * 100)  # centiseconds
        # Format the time string as required
        s = time.strftime("%M:%S", now) + f".{cs:02d}"
        return s

def setup_logging(level: int = logging.INFO) -> None:
    """
    Configures the root logger for console output with a custom format and level.

    Sets up a `StreamHandler` for the root logger if no handlers are already present.
    Applies a `CustomTimeFormatter` to display timestamps as MM:SS.cs and uses
    ANSI colors for different log message parts (timestamp, logger name, level, message).
    This setup avoids modifying global logging state like the record factory or
    the global Formatter converter.

    Args:
        level: The minimum logging level for the root logger and the console handler
               (e.g., `logging.DEBUG`, `logging.INFO`). Defaults to `logging.INFO`.
    """
    # Check if the root logger already has handlers to avoid adding them multiple times
    root_logger = logging.getLogger()
    if not root_logger.hasHandlers():
        # Set the level on the logger itself
        root_logger.setLevel(level)

        # --- Define Format String ---
        # Note: We will use the standard '%(asctime)s' placeholder now,
        # because our CustomTimeFormatter will format it correctly.
        prefix = Colors.apply("🖥️").gray
        # Use the standard %(asctime)s - our custom formatter will handle its appearance
        timestamp = Colors.apply("%(asctime)s").blue
        levelname = Colors.apply("%(levelname)-4.4s").green.bold
        message = Colors.apply("%(message)s")
        logger_name = Colors.apply("%(name)-10.10s").gray

        log_format = f"{timestamp} {logger_name} {levelname} {message}"
        # --- Format String Defined ---

        # --- Configure logging using explicit Handler/Formatter ---
        # Avoids basicConfig and global state modification

        # 1. Create the custom formatter instance
        # We pass the desired log format string. datefmt is not needed as formatTime is overridden.
        formatter = CustomTimeFormatter(log_format)

        # 2. Create a handler (e.g., StreamHandler to log to console)
        handler = logging.StreamHandler()

        # 3. Set the custom formatter on the handler
        handler.setFormatter(formatter)

        # 4. Set the level *on the handler* (optional but good practice,
        #    basicConfig does this implicitly). Controls messages processed by this handler.
        handler.setLevel(level)

        # 5. Add the configured handler to the root logger
        root_logger.addHandler(handler)

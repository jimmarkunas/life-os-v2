"""Routed Newsletter source processing."""
from .models import MessageParseResult, ParseState, RoutedNewsletterMessage, SourceVacancyObservation
from .parsers import detect_source, parse_message
from .processor import NewsletterExecutionState, NewsletterProcessResult, NewsletterProcessor, NewsletterSourcePort
__all__ = ["MessageParseResult","ParseState","RoutedNewsletterMessage","SourceVacancyObservation","detect_source","parse_message","NewsletterExecutionState","NewsletterProcessResult","NewsletterProcessor","NewsletterSourcePort"]

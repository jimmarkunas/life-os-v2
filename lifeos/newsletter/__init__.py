"""Routed Newsletter source processing."""
from .jobs_seam import JobsCandidateAdapter, adapt_for_jobs
from .models import MessageParseResult, ParseState, RoutedNewsletterMessage, SourceVacancyObservation
from .parsers import detect_source, parse_message
from .processor import NewsletterExecutionState, NewsletterProcessResult, NewsletterProcessor, NewsletterSourcePort
__all__ = ["JobsCandidateAdapter","adapt_for_jobs","MessageParseResult","ParseState","RoutedNewsletterMessage","SourceVacancyObservation","detect_source","parse_message","NewsletterExecutionState","NewsletterProcessResult","NewsletterProcessor","NewsletterSourcePort"]

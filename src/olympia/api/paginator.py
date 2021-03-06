from rest_framework.pagination import PageNumberPagination
from django.core.paginator import (
    EmptyPage, InvalidPage, Page, PageNotAnInteger, Paginator)


class ESPaginator(Paginator):
    """
    A better paginator for search results
    The normal Paginator does a .count() query and then a slice. Since ES
    results contain the total number of results, we can take an optimistic
    slice and then adjust the count.
    """

    # Maximum result position. Should match 'index.max_result_window' ES
    # setting if present. ES defaults to 10000 but we'd like more to make sure
    # all our extensions can be found if searching without a query and
    # paginating through all results.
    max_result_window = 25000

    def validate_number(self, number):
        """
        Validates the given 1-based page number.
        This class overrides the default behavior and ignores the upper bound.
        """
        try:
            number = int(number)
        except (TypeError, ValueError):
            raise PageNotAnInteger('That page number is not an integer')
        if number < 1:
            raise EmptyPage('That page number is less than 1')
        return number

    def page(self, number):
        """
        Returns a page object.
        This class overrides the default behavior and ignores "orphans" and
        assigns the count from the ES result to the Paginator.
        """
        number = self.validate_number(number)
        bottom = (number - 1) * self.per_page
        top = bottom + self.per_page

        if bottom > self.max_result_window:
            raise InvalidPage(
                'That page number is too high for the current page size')

        # Force the search to evaluate and then attach the count. We want to
        # avoid an extra useless query even if there are no results, so we
        # directly fetch the count from hits.
        # Overwrite `object_list` with the list of ES results.
        result = self.object_list[bottom:top].execute()
        page = Page(result.hits, number, self)
        # Update the `_count`.
        self._count = page.object_list.total

        # Now that we have the count validate that the page number isn't higher
        # than the possible number of pages and adjust accordingly.
        if number > self.num_pages:
            if number == 1 and self.allow_empty_first_page:
                pass
            else:
                raise EmptyPage('That page contains no results')

        return page


class CustomPageNumberPagination(PageNumberPagination):
    page_size_query_param = 'page_size'
    max_page_size = 50


class ESPageNumberPagination(CustomPageNumberPagination):
    """Custom pagination implementation to hook in our `ESPaginator`."""
    django_paginator_class = ESPaginator

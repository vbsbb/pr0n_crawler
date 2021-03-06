import asyncio
import logging
import time

import aiohttp
from inflection import humanize, parameterize
from lxml import html
from tenacity import before_log, retry, stop_after_attempt, wait_random

from src.finders import find_video_details, find_videos_title, find_videos_url, find_videos_thumbnail_url, \
    find_videos_duration, find_prev_page
from src.models import Video, Site, Tag, VideoTag
from src import tumblr


class CrawlerMixin(object):
    site_name = None  # type: str
    site_url = None  # type: str
    crawler_entry_point = None  # type: str
    crawler_selectors = dict()  # type: dict[str, str or dict[str, str]]

    def __init__(self):
        self.crawler_current_videos = 0
        self._hydrate_logger()

        if not (self.site_name or self.site_url):
            raise ValueError("Site's name and site's url should not be None.")

        self.site, created = Site.get_or_create(
            name=self.site_name,
            url=self.site_url
        )

        if created:
            self.logger.info('Site created.')

        self.created_videos_on_crawl = []

    async def crawl(self, url=None):
        """
        :type url: str
        :return:
        """

        should_continue_download = True

        if not url:
            url = self.site_url + self.crawler_entry_point

        while should_continue_download:
            self.created_videos_on_crawl = []
            [url, should_continue_download] = await self._download(url)

            for video in self.created_videos_on_crawl:
                tumblr.post(video)
        else:
            self.logger.info('%s has been crawled!' % self.site_name)

    @retry(
        stop=stop_after_attempt(20), wait=wait_random(8, 512),
        before=before_log(logging.getLogger(), logging.WARN)
    )
    async def _download(self, url):
        should_continue_download = True
        tree = await self._download_videos_page(url)
        videos = await self._find_videos_from_videos_page(url, tree)

        if not videos:
            raise ValueError('No videos found')

        try:
            prev_page = find_prev_page(tree, self.prev_page_selector)
            url = self.site_url + prev_page
        except IndexError:
            should_continue_download = False

        self.logger.info('-' * 60)
        return [url, should_continue_download]

    async def crawl_convert_video_duration_to_seconds(self, duration):
        """
        :type duration: str
        :rtype: int
        """

        raise NotImplementedError

    def _hydrate_logger(self):
        self.logger = logging.LoggerAdapter(logging.getLogger('pr0n_crawler'), {
            'site_name': self.site_name,
            'videos_current_number': self.crawler_current_videos,
        })

    async def _download_videos_page(self, url):
        """
        :type url: str
        :rtype: lxml.html.Element
        """

        self.logger.info('Downloading {}...'.format(url))

        time_start = time.time()
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 404:
                    self.logger.error('Can not download {}, got 404.'.format(url))
                    exit(1)  # TODO: not already handled case
                else:
                    self.logger.info('Downloaded in {:.3f} seconds.'.format(time.time() - time_start))
                    content = await response.text()
                    tree = html.fromstring(content)
                    return tree

    async def _find_videos_from_videos_page(self, url, tree):
        """
        :type url: str
        :type tree: lxml.html.Element
        :rtype: list[Video]
        """

        self.logger.info('Finding videos metadata from {}...'.format(url))
        time_start = time.time()

        videos_metadata = self._fetch_videos_page_and_find_metadata(tree)
        videos = self._get_or_create_videos_from_metadata(videos_metadata)
        await self._find_more_videos_metadata(videos)

        self.logger.info(
            'Found videos metadata for {} videos in {:.3f} seconds.'.format(
                len(videos),
                time.time() - time_start)
        )

        return videos

    def _fetch_videos_page_and_find_metadata(self, tree):
        """
        :param tree: lxml.html.Element
        :return: list of tuples following (title, url, thumbnail_url, durations) format
        :rtype: list[(str, str, str, int)]
        """

        titles = find_videos_title(tree, self.video_title_selector)
        urls = find_videos_url(tree, self.video_url_selector)
        thumbnail_urls = find_videos_thumbnail_url(tree, self.video_thumbnail_url_selector)
        durations = map(
            self.crawl_convert_video_duration_to_seconds,
            find_videos_duration(tree, self.video_duration_selector)
        )

        return list(zip(titles, urls, thumbnail_urls, durations))

    def _get_or_create_videos_from_metadata(self, videos_metadata):
        """
        :type videos_metadata: list[(str, str, str, int)]
        :rtype: list[Video]
        """
        videos = []

        for m in videos_metadata:
            video, created = Video.get_or_create(
                title=m[0].strip(),
                url=m[1].strip(),
                thumbnail_url=m[2].strip(),
                duration=m[3],
                site=self.site
            )
            videos.append(video)
            if created:
                self.created_videos_on_crawl.append(video)

        return videos

    async def _find_more_videos_metadata(self, videos):
        """
        :param videos: list[Video]
        """

        tasks = []

        for video in videos:
            tasks.append(self._fetch_video_page_and_find_metadata(video))

        await asyncio.gather(*tasks)

    @retry(
        stop=stop_after_attempt(20), wait=wait_random(8, 512),
        before=before_log(logging.getLogger(), logging.WARN)
    )
    async def _fetch_video_page_and_find_metadata(self, video):
        """
        :type video: Video
        """

        url = self.site_url + video.url
        self.logger.info('Downloading {}...'.format(url))

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 404:
                    self.logger.warning('Can not download {}'.format(url))
                    return

                content = await response.text()
                self.logger.info('Downloaded {}.'.format(url))
                tree = html.fromstring(content)

                details = find_video_details(tree, dict(
                    video_details_tags=self.video_details_tags_selector
                ))

                save_video_details(video, details)

                self.logger.info('Got details from {}'.format(url))
                self.crawler_current_videos += 1
                self._hydrate_logger()

    @property
    def video_title_selector(self):
        """:rtype: str"""
        return self.crawler_selectors.get('video').get('title')

    @property
    def video_duration_selector(self):
        """:rtype: str"""
        return self.crawler_selectors.get('video').get('duration')

    @property
    def video_url_selector(self):
        """:rtype: str"""
        return self.crawler_selectors.get('video').get('url')

    @property
    def video_thumbnail_url_selector(self):
        """:rtype: str"""
        return self.crawler_selectors.get('video').get('thumbnail_url')

    @property
    def video_details_tags_selector(self):
        """:rtype: str"""
        return self.crawler_selectors.get('video_details').get('tags')

    @property
    def prev_page_selector(self):
        """:rtype: str"""
        return self.crawler_selectors.get('prev_page')


def save_video_details(video, details):
    """
    :type video: Video
    :type details: dict
    """

    for found_tag in details.get('tags'):
        slug = parameterize(found_tag.strip())
        tag = humanize(slug)

        if not slug:
            continue

        tag, created = Tag.get_or_create(
            tag=tag,
            slug=slug
        )

        VideoTag.get_or_create(video=video, tag=tag)

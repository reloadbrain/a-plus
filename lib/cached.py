from django.core.cache import cache
import logging


logger = logging.getLogger("cached")


class CachedAbstract(object):
    KEY_PREFIX = 'abstract'

    @classmethod
    def _key(cls, *models, modifiers):
        return "{}:{}".format(cls.KEY_PREFIX, ",".join(
            [str(m.pk if hasattr(m, 'pk') else 0) for m in models]
            + modifiers
        ))

    @classmethod
    def invalidate(cls, *models, modifiers=[]):
        cache_key = cls._key(*models, modifiers=modifiers)
        logger.debug("Invalidating cached data for {}".format(cache_key))
        cache.delete(cache_key)

    def __init__(self, *models, modifiers=[]):
        cache_key = self.__class__._key(*models, modifiers=modifiers)
        data = cache.get(cache_key)
        if self._needs_generation(data):
            logger.debug("Generating cached data for {}".format(cache_key))
            self.dirty = False
            data = self._generate_data(*models, data=data)
            if not self.dirty:
                cache.set(cache_key, data, None)
        self.data = data

    def _needs_generation(self, data):
        return data is None

    def _generate_data(self, *models, data=None):
        # Insert the time consuming data generation.
        print("Failed by using CachedAbstract directly.")
        assert False

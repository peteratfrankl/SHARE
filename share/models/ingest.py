import contextlib
import datetime
import logging

from stevedore import driver

from django.contrib.postgres.fields import JSONField
from django.core import validators
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.db import DEFAULT_DB_ALIAS
from django.db import connection
from django.db import connections
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.deconstruct import deconstructible

from share.models.fields import EncryptedJSONField
from share.models.fuzzycount import FuzzyCountManager
from share.models.indexes import ConcurrentIndex
from share.util import chunked, placeholders, BaseJSONAPIMeta


logger = logging.getLogger(__name__)
__all__ = ('Source', 'RawDatum', 'SourceConfig', 'Harvester', 'Transformer', 'SourceUniqueIdentifier')


class SourceIcon(models.Model):
    source_name = models.TextField(unique=True)
    image = models.BinaryField()


@deconstructible
class SourceIconStorage(Storage):
    def _open(self, name, mode='rb'):
        assert mode == 'rb'
        icon = SourceIcon.objects.get(source_name=name)
        return ContentFile(icon.image)

    def _save(self, name, content):
        SourceIcon.objects.update_or_create(source_name=name, defaults={'image': content.read()})
        return name

    def delete(self, name):
        SourceIcon.objects.get(source_name=name).delete()

    def get_available_name(self, name, max_length=None):
        return name

    def url(self, name):
        return reverse('source_icon', kwargs={'source_name': name})


def icon_name(instance, filename):
    return instance.name


class NaturalKeyManager(models.Manager):
    use_in_migrations = True

    def __init__(self, *key_fields):
        super(NaturalKeyManager, self).__init__()
        self.key_fields = key_fields

    def get_by_natural_key(self, key):
        return self.get(**dict(zip(self.key_fields, key)))


class Source(models.Model):
    name = models.TextField(unique=True)
    long_title = models.TextField(unique=True)
    home_page = models.URLField(null=True, blank=True)
    icon = models.ImageField(upload_to=icon_name, storage=SourceIconStorage(), blank=True)
    is_deleted = models.BooleanField(default=False)

    # Whether or not this SourceConfig collects original content
    # If True changes made by this source cannot be overwritten
    # This should probably be on SourceConfig but placing it on Source
    # is much easier for the moment.
    # I also haven't seen a situation where a Source has two feeds that we harvest
    # where one provider unreliable metadata but the other does not.
    canonical = models.BooleanField(default=False, db_index=True)

    # TODO replace with object permissions, allow multiple sources per user (SHARE-996)
    user = models.OneToOneField('ShareUser', null=True, on_delete=models.CASCADE)

    objects = NaturalKeyManager('name')

    class JSONAPIMeta(BaseJSONAPIMeta):
        pass

    def natural_key(self):
        return (self.name,)

    def __repr__(self):
        return '<{}({}, {}, {})>'.format(self.__class__.__name__, self.pk, self.name, self.long_title)

    def __str__(self):
        return repr(self)


class SourceConfig(models.Model):
    # Previously known as the provider's app_label
    label = models.TextField(unique=True)
    version = models.PositiveIntegerField(default=1)

    source = models.ForeignKey('Source', on_delete=models.CASCADE, related_name='source_configs')
    base_url = models.URLField(null=True)
    earliest_date = models.DateField(null=True, blank=True)
    rate_limit_allowance = models.PositiveIntegerField(default=5)
    rate_limit_period = models.PositiveIntegerField(default=1)

    # Allow null for push sources
    harvester = models.ForeignKey('Harvester', null=True, on_delete=models.CASCADE)
    harvester_kwargs = JSONField(null=True, blank=True)
    harvest_interval = models.DurationField(default=datetime.timedelta(days=1))
    harvest_after = models.TimeField(default='02:00')
    full_harvest = models.BooleanField(default=False, help_text=(
        'Whether or not this SourceConfig should be fully harvested. '
        'Requires earliest_date to be set. '
        'The schedule harvests task will create all logs necessary if this flag is set. '
        'This should never be set to True by default. '
    ))

    # Allow null for push sources
    # TODO put pushed data through a transformer, add a JSONLDTransformer or something for backward compatibility
    transformer = models.ForeignKey('Transformer', null=True, on_delete=models.CASCADE)
    transformer_kwargs = JSONField(null=True, blank=True)

    disabled = models.BooleanField(default=False)

    private_harvester_kwargs = EncryptedJSONField(blank=True, null=True)
    private_transformer_kwargs = EncryptedJSONField(blank=True, null=True)

    objects = NaturalKeyManager('label')

    class JSONAPIMeta(BaseJSONAPIMeta):
        pass

    def natural_key(self):
        return (self.label,)

    def get_harvester(self, **kwargs):
        return self.harvester.get_class()(self, **{**kwargs, **(self.harvester_kwargs or {})})

    def get_transformer(self):
        return self.transformer.get_class()(self, **(self.transformer_kwargs or {}))

    @contextlib.contextmanager
    def acquire_lock(self, required=True, using='default'):
        from share.harvest.exceptions import HarvesterConcurrencyError

        # NOTE: Must be in transaction
        logger.debug('Attempting to lock %r', self)
        with connections[using].cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s::regclass::integer, %s);", (self._meta.db_table, self.id))
            locked = cursor.fetchone()[0]
            if not locked and required:
                logger.warning('Lock failed; another task is already harvesting %r.', self)
                raise HarvesterConcurrencyError('Unable to lock {!r}'.format(self))
            elif locked:
                logger.debug('Lock acquired on %r', self)
            else:
                logger.warning('Lock not acquired on %r', self)
            try:
                yield
            finally:
                if locked:
                    cursor.execute("SELECT pg_advisory_unlock(%s::regclass::integer, %s);", (self._meta.db_table, self.id))
                    logger.debug('Lock released on %r', self)

    def __repr__(self):
        return '<{}({}, {})>'.format(self.__class__.__name__, self.pk, self.label)

    def __str__(self):
        return '{}: {}'.format(self.source.long_title, self.label)


class Harvester(models.Model):
    key = models.TextField(unique=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_modified = models.DateTimeField(auto_now=True)

    objects = NaturalKeyManager('key')

    @property
    def version(self):
        return self.get_class().VERSION

    def natural_key(self):
        return (self.key,)

    def get_class(self):
        return driver.DriverManager('share.harvesters', self.key).driver

    def __repr__(self):
        return '<{}({}, {})>'.format(self.__class__.__name__, self.pk, self.key)

    def __str__(self):
        return repr(self)


class Transformer(models.Model):
    key = models.TextField(unique=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_modified = models.DateTimeField(auto_now=True)

    objects = NaturalKeyManager('key')

    @property
    def version(self):
        return self.get_class().VERSION

    def natural_key(self):
        return (self.key,)

    def get_class(self):
        return driver.DriverManager('share.transformers', self.key).driver

    def __repr__(self):
        return '<{}({}, {})>'.format(self.__class__.__name__, self.pk, self.key)

    def __str__(self):
        return repr(self)


class RawDatumManager(FuzzyCountManager):

    def link_to_log(self, log, datum_ids):
        if not datum_ids:
            return True
        logger.debug('Linking RawData to %r', log)
        with connection.cursor() as cursor:
            for chunk in chunked(datum_ids, size=500):
                if not chunk:
                    break
                cursor.execute('''
                    INSERT INTO "{table}"
                        ("{rawdatum}", "{harvestlog}")
                    VALUES
                        {values}
                    ON CONFLICT ("{rawdatum}", "{harvestlog}") DO NOTHING;
                '''.format(
                    values=', '.join('%s' for _ in range(len(chunk))),  # Nasty hack. Fix when psycopg2 2.7 is released with execute_values
                    table=RawDatum.logs.through._meta.db_table,
                    rawdatum=RawDatum.logs.through._meta.get_field('rawdatum').column,
                    harvestlog=RawDatum.logs.through._meta.get_field('harvestlog').column,
                ), [(raw_id, log.id) for raw_id in chunk])
        return True

    def store_chunk(self, source_config, data, limit=None, db=DEFAULT_DB_ALIAS):
        """Store a large amount of data for a single source_config.

        Data MUST be a utf-8 encoded string (Just a str type).
        Take special care to make sure you aren't destroying data by mis-encoding it.

        Args:
            source_config (SourceConfig):
            data Generator[(str, str)]: (identifier, datum)

        Returns:
            Generator[RawDatum]
        """
        hashes = {}
        identifiers = {}
        now = timezone.now()

        if limit == 0:
            return []

        for chunk in chunked(data, 500):
            if not chunk:
                break

            new = []
            new_identifiers = set()
            for fr in chunk:
                if limit and len(hashes) >= limit:
                    break

                if fr.sha256 in hashes:
                    if hashes[fr.sha256] != fr.identifier:
                        raise ValueError(
                            '{!r} has already been seen or stored with identifier "{}". '
                            'Perhaps your identifier extraction is incorrect?'.format(fr, hashes[fr.sha256])
                        )
                    logger.warning('Recieved duplicate datum %s from %s', fr, source_config)
                    continue

                new.append(fr)
                hashes[fr.sha256] = fr.identifier
                new_identifiers.add(fr.identifier)

            if new_identifiers:
                suids = SourceUniqueIdentifier.objects.raw('''
                    INSERT INTO "{table}"
                        ("{identifier}", "{source_config}")
                    VALUES
                        {values}
                    ON CONFLICT
                        ("{identifier}", "{source_config}")
                    DO UPDATE SET
                        id = "{table}".id
                    RETURNING {fields}
                '''.format(
                    table=SourceUniqueIdentifier._meta.db_table,
                    identifier=SourceUniqueIdentifier._meta.get_field('identifier').column,
                    source_config=SourceUniqueIdentifier._meta.get_field('source_config').column,
                    values=placeholders(len(new_identifiers)),  # Nasty hack. Fix when psycopg2 2.7 is released with execute_values
                    fields=', '.join('"{}"'.format(field.column) for field in SourceUniqueIdentifier._meta.concrete_fields),
                ), [(identifier, source_config.id) for identifier in new_identifiers])

                for suid in suids:
                    identifiers[suid.identifier] = suid.pk

            if new:
                # Defer 'datum' by omitting it from the returned fields
                yield from RawDatum.objects.raw(
                    '''
                        INSERT INTO "{table}"
                            ("{suid}", "{hash}", "{datum}", "{datestamp}", "{date_modified}", "{date_created}")
                        VALUES
                            {values}
                        ON CONFLICT
                            ("{suid}", "{hash}")
                        DO UPDATE SET
                            "{datestamp}" = EXCLUDED."{datestamp}",
                            "{date_modified}" = EXCLUDED."{date_modified}"
                        RETURNING id, "{suid}", "{hash}", "{datestamp}", "{date_modified}", "{date_created}"
                    '''.format(
                        table=RawDatum._meta.db_table,
                        suid=RawDatum._meta.get_field('suid').column,
                        hash=RawDatum._meta.get_field('sha256').column,
                        datum=RawDatum._meta.get_field('datum').column,
                        datestamp=RawDatum._meta.get_field('datestamp').column,
                        date_modified=RawDatum._meta.get_field('date_modified').column,
                        date_created=RawDatum._meta.get_field('date_created').column,
                        values=', '.join('%s' for _ in range(len(new))),  # Nasty hack. Fix when psycopg2 2.7 is released with execute_values
                    ), [
                        (identifiers[fr.identifier], fr.sha256, fr.datum, fr.datestamp, now, now)
                        for fr in new
                    ]
                )

            if limit and len(hashes) >= limit:
                break

    def store_data(self, config, fetch_result):
        """
        """
        (rd, ) = self.store_chunk(config, [fetch_result])

        if rd.created:
            logger.debug('New %r', rd)
        else:
            logger.debug('Found existing %r', rd)

        return rd


class SourceUniqueIdentifier(models.Model):
    identifier = models.TextField()
    source_config = models.ForeignKey('SourceConfig', on_delete=models.CASCADE)

    class Meta:
        unique_together = ('identifier', 'source_config')

    def __str__(self):
        return '{} {}'.format(self.source_config_id, self.identifier)

    def __repr__(self):
        return '<{}({}, {!r})>'.format(self.__class__.__name__, self.source_config_id, self.identifier)


class RawDatum(models.Model):

    datum = models.TextField()

    suid = models.ForeignKey(SourceUniqueIdentifier, on_delete=models.CASCADE)

    # The sha256 of the datum
    sha256 = models.TextField(validators=[validators.MaxLengthValidator(64)])

    datestamp = models.DateTimeField(null=True, help_text=(
        'The most relevant datetime that can be extracted from this RawDatum. '
        'This may be, but is not limitted to, a deletion, modification, publication, or creation datestamp. '
        'Ideally, this datetime should be appropriate for determining the chronological order it\'s data will be applied.'
    ))

    date_modified = models.DateTimeField(auto_now=True, editable=False)
    date_created = models.DateTimeField(auto_now_add=True, editable=False)

    no_output = models.NullBooleanField(null=True, help_text=(
        'Indicates that this RawDatum resulted in an empty graph when transformed. '
        'This allows the RawDataJanitor to find records that have not been processed. '
        'Records that result in an empty graph will not have a NormalizedData associated with them, '
        'which would otherwise look like data that has not yet been processed.'
    ))

    logs = models.ManyToManyField('HarvestLog', related_name='raw_data')

    objects = RawDatumManager()

    @property
    def created(self):
        return self.date_modified == self.date_created

    class Meta:
        unique_together = ('suid', 'sha256')
        verbose_name_plural = 'Raw Data'
        indexes = (
            ConcurrentIndex(fields=['no_output']),
        )

    class JSONAPIMeta(BaseJSONAPIMeta):
        resource_name = 'RawData'

    def __repr__(self):
        return '<{}({}, {}, {}...)>'.format(self.__class__.__name__, self.suid_id, self.datestamp, self.sha256[:10])

    __str__ = __repr__

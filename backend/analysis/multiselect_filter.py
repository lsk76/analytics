"""
Multi-select and Autocomplete filters for Django Admin.

Provides reusable components for creating admin filters with multiple selection support.

Usage Examples:
    
    # 1. Simplest approach - auto-generates parameter names:
    StatusFilter = multiselect_filter(Status, 'Status', 'status')
    
    # 2. With custom queryset and labels:
    ActiveUsersFilter = multiselect_filter(
        model=User,
        title='Active Users',
        field_name='user',
        queryset_callback=lambda qs: qs.filter(is_active=True),
        label_callback=lambda u: f"{u.last_name} {u.name}",
        ordering='username'
    )
    
    # 3. Full control:
    CustomFilter = create_multiselect_filter(
        model=MyModel,
        title='Custom',
        parameter_name='my_param',
        filter_field='my_field__in',
        queryset_callback=lambda qs: qs.custom_filter(),
        label_callback=lambda obj: obj.custom_label(),
        ordering='name'
    )
    
    # 4. Complex logic - extend MultiSelectFilter:
    class RBACFilter(MultiSelectFilter):
        def lookups(self, request, model_admin):
            if request.user.is_superuser:
                return all_items
            return filtered_items
    
    # 5. Autocomplete filter using Django admin autocomplete (recommended):
    NodeFilter = autocomplete_filter(
        title='Nodes',
        parameter_name='node_id',
        filter_field='node__id__in',
        selected_lookup=lambda request, ids: Node.objects.filter(id__in=ids),
        admin_autocomplete_field='node'  # uses /admin/autocomplete/
    )
    
    # 6. Complex autocomplete - extend AutocompleteFilter:
    class CustomNodeFilter(AutocompleteFilter):
        title = _('Custom Nodes')
        parameter_name = 'node_id'
        admin_autocomplete_field = 'node'  # uses Django admin autocomplete
        
        def get_selected_options(self, request, selected_ids):
            return Node.objects.filter(id__in=selected_ids)
        
        def filter_queryset(self, queryset, values):
            return queryset.filter(node__id__in=values)
"""
from typing import Optional, Callable, Any
from django.contrib import admin
from django.db.models import Model, QuerySet
from django.utils.translation import gettext_lazy as _


class MultiSelectFilter(admin.SimpleListFilter):
    """
    Base class for multi-select filters with checkbox support.
    
    Usage:
        class MyFilter(MultiSelectFilter):
            title = _('My Filter')
            parameter_name = 'my_param'
            
            def lookups(self, request, model_admin):
                # Return list of (value, label) tuples
                return [
                    ('option1', 'Option 1'),
                    ('option2', 'Option 2'),
                ]
            
            def filter_queryset(self, queryset, values):
                # Filter queryset by selected values
                return queryset.filter(field__in=values)
    
    Features:
        - Multiple value selection via checkboxes
        - JavaScript-based URL updates
        - Custom template for checkbox rendering
        - Subclasses only need to implement lookups() and filter_queryset()
    """
    template = 'admin/filters/multi_select.html'
    
    def __init__(self, request, params, model, model_admin):
        super().__init__(request, params, model, model_admin)
        self.request = request  # Store request for later use in choices()
    
    def choices(self, changelist):
        """Generate choices with multi-select support"""
        # Get currently selected values from stored request
        selected_values = self.request.GET.getlist(self.parameter_name)
        
        # Get lookup choices
        self.lookup_choices = self.lookups(self.request, changelist.model_admin)
        
        # Yield "All" option
        yield {
            'selected': len(selected_values) == 0,
            'query_string': changelist.get_query_string(remove=[self.parameter_name]),
            'display': _('All'),
            'value': '__all__',
        }
        
        # Yield individual choices
        for lookup, title in self.lookup_choices:
            yield {
                'selected': lookup in selected_values,
                'query_string': '',  # URL will be built by JS
                'display': title,
                'value': lookup,
            }
    
    def queryset(self, request, queryset):
        """Filter queryset by selected values (supports multiple)"""
        # Get selected values (can be multiple) from stored request
        values = self.request.GET.getlist(self.parameter_name)
        if values:
            return self.filter_queryset(queryset, values).distinct()
        return queryset
    
    def filter_queryset(self, queryset, values):
        """
        Override this method to define how to filter the queryset.
        
        Args:
            queryset: The base queryset to filter
            values: List of selected values from the filter
        
        Returns:
            Filtered queryset
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement filter_queryset() method"
        )


def multiselect_filter(
    model: type[Model],
    title: str,
    field_name: Optional[str] = None,
    **kwargs
) -> type[admin.SimpleListFilter]:
    """
    Simplified factory function for common cases.
    
    Args:
        model: The model to filter by
        title: Filter title
        field_name: Field name (auto-generates parameter and filter field)
        **kwargs: Additional arguments for create_multiselect_filter
    
    Returns:
        A configured MultiSelectFilter class
    
    Example:
        # Simplest usage - auto-generates everything
        StatusFilter = multiselect_filter(Status, 'Status', 'status')
        
        # Equivalent to:
        StatusFilter = create_multiselect_filter(
            model=Status,
            title='Status',
            parameter_name='status_id',
            filter_field='status_id__in'
        )
    """
    if field_name:
        parameter_name = kwargs.get('parameter_name', f'{field_name}_id')
        filter_field = kwargs.get('filter_field', f'{field_name}_id__in')
    else:
        parameter_name = kwargs.get('parameter_name')
        filter_field = kwargs.get('filter_field')
    
    return create_multiselect_filter(
        model=model,
        title=title,
        parameter_name=parameter_name,
        filter_field=filter_field,
        queryset_callback=kwargs.get('queryset_callback'),
        label_callback=kwargs.get('label_callback'),
        ordering=kwargs.get('ordering', 'name')
    )


def create_multiselect_filter(
    model: type[Model],
    title: str,
    parameter_name: str,
    filter_field: str,
    queryset_callback: Optional[Callable[[QuerySet], QuerySet]] = None,
    label_callback: Optional[Callable[[Any], str]] = None,
    ordering: str = 'name'
) -> type[admin.SimpleListFilter]:
    """
    Factory function to create a MultiSelect filter class.
    
    Args:
        model: The model to filter by (e.g., ActionType, Mission)
        title: Filter title displayed in admin
        parameter_name: URL parameter name
        filter_field: Field to filter on (e.g., 'action_type_id__in')
        queryset_callback: Optional function to customize queryset
        label_callback: Optional function to format labels
        ordering: Field to order by (default: 'name')
    
    Returns:
        A configured MultiSelectFilter class
    
    Example:
        ActionTypeFilter = create_multiselect_filter(
            model=ActionType,
            title='Action Type',
            parameter_name='action_type_id',
            filter_field='action_type_id__in'
        )
    """
    
    class GenericMultiSelectFilter(MultiSelectFilter):
        """Dynamically created MultiSelect filter - inherits all base functionality"""
        
        def lookups(self, request, model_admin):
            """Auto-generate lookups from model"""
            qs = model.objects.all().order_by(ordering)
            
            if queryset_callback:
                qs = queryset_callback(qs)
            
            if label_callback:
                return [(str(obj.id), label_callback(obj)) for obj in qs]
            else:
                return [(str(obj.id), str(obj)) for obj in qs]
        
        def filter_queryset(self, queryset, values):
            """Filter using the configured filter_field"""
            return queryset.filter(**{filter_field: values})
    
    # Set class attributes
    GenericMultiSelectFilter.title = title
    GenericMultiSelectFilter.parameter_name = parameter_name
    GenericMultiSelectFilter.__name__ = f'{model.__name__}MultiSelectFilter'
    
    return GenericMultiSelectFilter


# ===========================================
# AUTOCOMPLETE FILTER
# ===========================================


class AutocompleteFilter(admin.SimpleListFilter):
    """
    Base class for autocomplete filters with AJAX search support.
    
    Usage:
        # Option 1: Use Django admin's built-in autocomplete (recommended)
        class NodeFilter(AutocompleteFilter):
            title = _('Nodes')
            parameter_name = 'node_id'
            admin_autocomplete_field = 'target_node'  # field name in model
            placeholder = 'Search nodes...'
            
            def get_selected_options(self, request, selected_ids):
                return Node.objects.filter(id__in=selected_ids)
            
            def filter_queryset(self, queryset, values):
                return queryset.filter(target_node__id__in=values)
        
        # Option 2: Use custom autocomplete URL
        class NodeFilter(AutocompleteFilter):
            title = _('Nodes')
            parameter_name = 'node_id'
            autocomplete_url_name = 'myapp:node_autocomplete'
            placeholder = 'Search nodes...'
            ...
    
    Features:
        - AJAX-based autocomplete search via Select2
        - Multiple value selection
        - Pre-populated selected values on page load
        - Supports Django admin autocomplete or custom endpoints
        - Subclasses implement get_selected_options() and filter_queryset()
    """
    template = 'admin/filters/autocomplete_select.html'
    autocomplete_url_name: str = ''  # URL name for custom autocomplete endpoint
    admin_autocomplete_field: str = ''  # Field name for Django admin autocomplete
    placeholder: str = 'Пошук...'
    
    def __init__(self, request, params, model, model_admin):
        self.request = request
        self.model = model
        self.model_admin = model_admin
        
        # Resolve autocomplete URL
        if self.admin_autocomplete_field:
            # Use Django admin's built-in autocomplete
            from django.urls import reverse
            self.autocomplete_url = (
                f"{reverse('admin:autocomplete')}"
                f"?app_label={model._meta.app_label}"
                f"&model_name={model._meta.model_name}"
                f"&field_name={self.admin_autocomplete_field}"
            )
        elif self.autocomplete_url_name:
            # Use custom autocomplete URL
            from django.urls import reverse
            self.autocomplete_url = reverse(self.autocomplete_url_name)
        
        super().__init__(request, params, model, model_admin)
    
    @property
    def has_selection(self):
        """Check if any values are selected"""
        return bool(self.request.GET.getlist(self.parameter_name))
    
    def has_output(self):
        """Always show autocomplete filter (even with no selections)"""
        return True
    
    def lookups(self, request, model_admin):
        """Return currently selected items for pre-populating the select"""
        selected_ids = request.GET.getlist(self.parameter_name)
        if selected_ids:
            selected_objects = self.get_selected_options(request, selected_ids)
            return [(str(self._get_object_id(obj)), self._get_object_label(obj)) for obj in selected_objects]
        return []
    
    def _get_object_id(self, obj):
        """Get object ID - override for custom ID field"""
        return obj.id
    
    def _get_object_label(self, obj):
        """Get object label - override for custom label"""
        return str(obj)
    
    def get_selected_options(self, request, selected_ids):
        """
        Return queryset/list of selected objects for display.
        
        Args:
            request: The HTTP request
            selected_ids: List of selected IDs from URL params
        
        Returns:
            Queryset or list of objects
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement get_selected_options() method"
        )
    
    def choices(self, changelist):
        """Generate choices with proper selected state"""
        selected_values = self.request.GET.getlist(self.parameter_name)
        
        # "All" option
        yield {
            'selected': len(selected_values) == 0,
            'query_string': changelist.get_query_string(remove=[self.parameter_name]),
            'display': _('All'),
            'value': '__all__',
        }
        
        # Selected items
        self.lookup_choices = self.lookups(self.request, changelist.model_admin)
        for lookup, title in self.lookup_choices:
            if lookup:
                yield {
                    'selected': str(lookup) in selected_values,
                    'query_string': '',
                    'display': title,
                    'value': lookup,
                }
    
    def queryset(self, request, queryset):
        """Filter queryset by selected values"""
        values = request.GET.getlist(self.parameter_name)
        if values:
            return self.filter_queryset(queryset, values).distinct()
        return queryset
    
    def filter_queryset(self, queryset, values):
        """
        Override this method to define how to filter the queryset.
        
        Args:
            queryset: The base queryset to filter
            values: List of selected values from the filter
        
        Returns:
            Filtered queryset
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement filter_queryset() method"
        )


def autocomplete_filter(
    title: str,
    parameter_name: str,
    filter_field: str,
    selected_lookup: Callable[[Any, list], QuerySet],
    autocomplete_url_name: str = '',
    admin_autocomplete_field: str = '',
    placeholder: str = 'Пошук...',
    label_callback: Optional[Callable[[Any], str]] = None,
) -> type[admin.SimpleListFilter]:
    """
    Factory function to create an Autocomplete filter class.
    
    Args:
        title: Filter title displayed in admin
        parameter_name: URL parameter name
        filter_field: Field to filter on (e.g., 'node__id__in')
        selected_lookup: Function(request, ids) -> queryset of selected objects
        autocomplete_url_name: Django URL name for custom autocomplete endpoint
        admin_autocomplete_field: Field name for Django admin autocomplete (recommended)
        placeholder: Placeholder text for search input
        label_callback: Optional function to format object labels
    
    Returns:
        A configured AutocompleteFilter class
    
    Example using Django admin autocomplete (recommended):
        NodeFilter = autocomplete_filter(
            title='Nodes',
            parameter_name='node_id',
            filter_field='target_node__id__in',
            selected_lookup=lambda req, ids: DossierNode.objects.filter(id__in=ids),
            admin_autocomplete_field='target_node',  # uses /admin/autocomplete/
            placeholder='Search node...'
        )
    
    Example using custom autocomplete URL:
        NodeFilter = autocomplete_filter(
            title='Nodes',
            parameter_name='node_id',
            filter_field='target_node__id__in',
            selected_lookup=lambda req, ids: DossierNode.objects.filter(id__in=ids),
            autocomplete_url_name='dossier:node_autocomplete',
            placeholder='Search node...'
        )
    """
    
    class GenericAutocompleteFilter(AutocompleteFilter):
        """Dynamically created Autocomplete filter"""
        
        def get_selected_options(self, request, selected_ids):
            """Get selected objects using the provided lookup function"""
            return selected_lookup(request, selected_ids)
        
        def _get_object_label(self, obj):
            """Get object label"""
            if label_callback:
                return label_callback(obj)
            return str(obj)
        
        def filter_queryset(self, queryset, values):
            """Filter using the configured filter_field"""
            return queryset.filter(**{filter_field: values})
    
    # Set class attributes
    GenericAutocompleteFilter.title = title
    GenericAutocompleteFilter.parameter_name = parameter_name
    GenericAutocompleteFilter.autocomplete_url_name = autocomplete_url_name
    GenericAutocompleteFilter.admin_autocomplete_field = admin_autocomplete_field
    GenericAutocompleteFilter.placeholder = placeholder
    GenericAutocompleteFilter.__name__ = f'{parameter_name.title().replace("_", "")}AutocompleteFilter'
    
    return GenericAutocompleteFilter


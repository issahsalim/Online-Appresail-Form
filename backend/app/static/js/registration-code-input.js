document.querySelectorAll('[data-code-pin]').forEach((control) => {
    const input = control.querySelector('[data-code-pin-input]');
    const slots = control.querySelectorAll('[data-code-pin-slot]');

    if (!input || slots.length !== 6) return;

    control.classList.add('is-enhanced');

    const updateSlots = () => {
        const digits = input.value.replace(/\D/g, '').slice(0, 6);
        if (input.value !== digits) input.value = digits;

        slots.forEach((slot, index) => {
            slot.textContent = digits[index] || '';
        });
    };

    input.addEventListener('input', updateSlots);
    updateSlots();
});
